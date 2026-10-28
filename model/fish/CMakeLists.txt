cmake_minimum_required(VERSION 3.20)
project(FISH VERSION 1.0.0 LANGUAGES C CXX)

# FISH is a self-contained inference engine: its own GGUF reader, quantised
# kernels, tokenizer, sampler, KV cache, memory budget and planner. Nothing
# here links llama.cpp or any other inference runtime.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(FISH_BUILD_SERVER "Build the standalone HTTP server" ON)
option(FISH_BUILD_TESTS  "Build the C++ acceptance tests" ON)

# ---------------------------------------------------------------------------
# Core engine sources.
#
# The older skeleton files (engine.cpp, hardware_probe.cpp, layer_planner.cpp,
# resident_window.cpp, prefetcher.cpp, expert_streamer.cpp, adapter_mgr.cpp,
# compression_policy.cpp, platform.cpp) are superseded by model/specs/c_api and
# are deliberately not compiled; keeping dead code in the binary only makes the
# import surface larger and the library harder to certify.
# ---------------------------------------------------------------------------
set(FISH_CORE
    src/gguf.cpp
    src/quant.cpp
    src/ops.cpp
    src/threadpool.cpp
    src/model.cpp
    src/tokenizer.cpp
    src/sampler.cpp
    src/specs.cpp
    src/c_api.cpp
)

if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|AMD64|amd64|i.86)$")
    list(APPEND FISH_CORE src/quant_x86.cpp)
    set(FISH_X86 1)
else()
    set(FISH_X86 0)
endif()

add_library(fish SHARED ${FISH_CORE})
target_include_directories(fish PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_definitions(fish PRIVATE FISH_HAVE_X86_KERNELS=${FISH_X86})
if(WIN32)
    # MinGW would otherwise emit libfish.dll; the product loads fish.dll.
    set_target_properties(fish PROPERTIES PREFIX "")
endif()

if(WIN32)
    # windres is driven explicitly: the RC-language rule does not quote -I
    # paths, and this workspace path contains a space. The version resource is
    # what makes the DLL look like a product to Windows instead of an
    # anonymous blob.
    find_program(FISH_WINDRES windres REQUIRED)
    set(FISH_RC_OBJ ${CMAKE_CURRENT_BINARY_DIR}/fish_res.o)
    add_custom_command(
        OUTPUT ${FISH_RC_OBJ}
        COMMAND ${FISH_WINDRES}
                "${CMAKE_CURRENT_SOURCE_DIR}/fish.rc" -O coff -o "${FISH_RC_OBJ}"
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/fish.rc
        COMMENT "Embedding FISH version resource"
        VERBATIM)
    target_sources(fish PRIVATE ${FISH_RC_OBJ})
    set_source_files_properties(${FISH_RC_OBJ} PROPERTIES EXTERNAL_OBJECT TRUE GENERATED TRUE)
endif()

if(MSVC)
    target_compile_options(fish PRIVATE /W3 /O2)
    if(FISH_X86)
        set_source_files_properties(src/quant_x86.cpp PROPERTIES COMPILE_OPTIONS "/arch:AVX2")
    endif()
else()
    target_compile_options(fish PRIVATE -O3 -Wall -Wextra -Wno-unused-parameter)
    if(MINGW)
        # One self-contained DLL: no libwinpthread / libstdc++ / libgcc to ship.
        target_link_options(fish PRIVATE -static)
        target_link_options(fish PRIVATE $<$<CONFIG:Release>:-s>)
    else()
        set_target_properties(fish PROPERTIES
            CXX_VISIBILITY_PRESET hidden
            VISIBILITY_INLINES_HIDDEN ON)
    endif()
endif()

# ---------------------------------------------------------------------------
# Standalone server (Linux / containers). On a desktop the Python host loads
# the shared library in-process instead.
# ---------------------------------------------------------------------------
if(FISH_BUILD_SERVER)
    add_executable(fish_server src/server.cpp)
    target_link_libraries(fish_server PRIVATE fish)
    if(WIN32)
        target_link_libraries(fish_server PRIVATE ws2_32)
    endif()
    if(MINGW)
        target_link_options(fish_server PRIVATE -static)
    endif()
endif()

# ---------------------------------------------------------------------------
# Acceptance tests. These compile the engine sources directly so they can
# reach the internal C++ API (GgufFile, vec_dot, ...) without exporting it.
# ---------------------------------------------------------------------------
if(FISH_BUILD_TESTS)
    set(FISH_TEST_SOURCES ${FISH_CORE})
    list(REMOVE_ITEM FISH_TEST_SOURCES src/c_api.cpp)

    foreach(name m1 m2)
        if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/test/test_${name}.cpp)
            add_executable(fish_test_${name} test/test_${name}.cpp ${FISH_TEST_SOURCES})
            target_include_directories(fish_test_${name} PRIVATE include)
            target_compile_definitions(fish_test_${name} PRIVATE FISH_HAVE_X86_KERNELS=${FISH_X86})
            if(MINGW)
                target_link_options(fish_test_${name} PRIVATE -static)
            endif()
        endif()
    endforeach()
endif()

install(TARGETS fish
        RUNTIME DESTINATION bin
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib)
if(FISH_BUILD_SERVER)
    install(TARGETS fish_server RUNTIME DESTINATION bin)
endif()
