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
    src/quantize.cpp
    src/lora.cpp
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

# ---------------------------------------------------------------------------
# Vulkan GPU backend (docs/engine/GPU.md).
#
# Needs only the Vulkan *headers* at build time: the loader is opened at
# runtime, so fish.dll still loads on machines without any Vulkan driver.
# Shaders are compiled with glslc and embedded as word arrays. If glslc is
# missing or does not run, the committed shaders/prebuilt/fish_vk_shaders.inc
# is used instead. Without headers the backend is switched off with a message.
# ---------------------------------------------------------------------------
option(FISH_VULKAN "Vulkan GPU backend" ON)
set(FISH_VK_ENABLED OFF)
set(FISH_VK_SHADER_NAMES)
if(FISH_VULKAN)
    set(_vk_roots)
    if(DEFINED ENV{VULKAN_SDK})
        file(TO_CMAKE_PATH "$ENV{VULKAN_SDK}" _vk_env)
        list(APPEND _vk_roots "${_vk_env}")
    endif()
    file(GLOB _vk_sdks LIST_DIRECTORIES true "C:/VulkanSDK/*")
    list(SORT _vk_sdks ORDER DESCENDING)
    list(APPEND _vk_roots ${_vk_sdks})
    set(_vk_inc_hints)
    set(_vk_bin_hints)
    foreach(_r IN LISTS _vk_roots)
        list(APPEND _vk_inc_hints "${_r}/Include" "${_r}/include")
        list(APPEND _vk_bin_hints "${_r}/Bin" "${_r}/bin")
    endforeach()
    find_path(FISH_VK_INCLUDE_DIR vulkan/vulkan.h HINTS ${_vk_inc_hints})
    find_program(FISH_GLSLC glslc HINTS ${_vk_bin_hints})

    if(NOT FISH_VK_INCLUDE_DIR)
        message(STATUS "FISH: Vulkan headers not found (install the Vulkan SDK or set VULKAN_SDK) - GPU backend disabled")
    else()
        set(FISH_VK_ENABLED ON)
        set(_vk_kernels matvec matmul_tiled)
        set(_vk_types F32 F16 Q4_0 Q8_0 Q4_K Q5_K Q6_K FQ3 FQ3S)   # FQ3 / FQ3S: re-quantised 3-bit (src/gpu/requant.h)
        foreach(_k IN LISTS _vk_kernels)
            foreach(_t IN LISTS _vk_types)
                list(APPEND FISH_VK_SHADER_NAMES ${_k}_${_t})
            endforeach()
        endforeach()
        # Layer-span kernels (one variant each, no weight type).
        set(_vk_span span_rmsnorm span_add span_rope span_kv_store span_attention span_act_mul)
        list(APPEND FISH_VK_SHADER_NAMES ${_vk_span})

        set(_glslc_ok FALSE)
        if(FISH_GLSLC)
            # Some SDK builds crash against an old system MSVCP140.dll; probe it.
            execute_process(COMMAND "${FISH_GLSLC}" --version
                            RESULT_VARIABLE _glslc_rc OUTPUT_QUIET ERROR_QUIET)
            if(_glslc_rc EQUAL 0)
                set(_glslc_ok TRUE)
            else()
                message(STATUS "FISH: ${FISH_GLSLC} does not run (exit ${_glslc_rc}); using prebuilt SPIR-V. "
                               "On Windows this is usually an outdated VC++ runtime; pass -DFISH_GLSLC=<working glslc> to rebuild shaders.")
            endif()
        endif()

        set(_sh ${CMAKE_CURRENT_SOURCE_DIR}/shaders)
        set(FISH_VK_PREBUILT ${_sh}/prebuilt/fish_vk_shaders.inc)
        if(_glslc_ok)
            set(_spv_dir ${CMAKE_CURRENT_BINARY_DIR}/spv)
            file(MAKE_DIRECTORY ${_spv_dir})
            set(_spv_files)
            foreach(_k IN LISTS _vk_kernels)
                foreach(_t IN LISTS _vk_types)
                    set(_out ${_spv_dir}/${_k}_${_t}.spv)
                    add_custom_command(
                        OUTPUT ${_out}
                        COMMAND "${FISH_GLSLC}" --target-env=vulkan1.1 -O -DTYPE_${_t}
                                -o "${_out}" "${_sh}/${_k}.comp"
                        DEPENDS ${_sh}/${_k}.comp ${_sh}/dequant.glsl
                        COMMENT "glslc ${_k}.comp (${_t})"
                        VERBATIM)
                    list(APPEND _spv_files ${_out})
                endforeach()
            endforeach()
            foreach(_k IN LISTS _vk_span)
                set(_out ${_spv_dir}/${_k}.spv)
                add_custom_command(
                    OUTPUT ${_out}
                    COMMAND "${FISH_GLSLC}" --target-env=vulkan1.1 -O -o "${_out}" "${_sh}/${_k}.comp"
                    DEPENDS ${_sh}/${_k}.comp
                    COMMENT "glslc ${_k}.comp"
                    VERBATIM)
                list(APPEND _spv_files ${_out})
            endforeach()
            set(FISH_VK_INC_DIR ${CMAKE_CURRENT_BINARY_DIR}/vk_gen)
            set(FISH_VK_INC ${FISH_VK_INC_DIR}/fish_vk_shaders.inc)
            string(REPLACE ";" "\\;" _names_arg "${FISH_VK_SHADER_NAMES}")
            add_custom_command(
                OUTPUT ${FISH_VK_INC}
                COMMAND ${CMAKE_COMMAND} -DOUT=${FISH_VK_INC} -DSPV_DIR=${_spv_dir}
                        "-DNAMES=${FISH_VK_SHADER_NAMES}" -P ${_sh}/embed_spv.cmake
                DEPENDS ${_spv_files} ${_sh}/embed_spv.cmake
                COMMENT "Embedding SPIR-V"
                VERBATIM)
            # `ninja fish_vk_prebuilt` refreshes the committed fallback copy.
            add_custom_target(fish_vk_prebuilt
                COMMAND ${CMAKE_COMMAND} -E copy_if_different ${FISH_VK_INC} ${FISH_VK_PREBUILT}
                DEPENDS ${FISH_VK_INC}
                COMMENT "Updating shaders/prebuilt/fish_vk_shaders.inc"
                VERBATIM)
            message(STATUS "FISH: Vulkan backend ON (shaders compiled with ${FISH_GLSLC})")
        elseif(EXISTS ${FISH_VK_PREBUILT})
            set(FISH_VK_INC_DIR ${_sh}/prebuilt)
            set(FISH_VK_INC ${FISH_VK_PREBUILT})
            message(STATUS "FISH: Vulkan backend ON (prebuilt SPIR-V)")
        else()
            set(FISH_VK_ENABLED OFF)
            message(STATUS "FISH: no working glslc and no prebuilt SPIR-V - GPU backend disabled")
        endif()
    endif()
endif()

if(FISH_VK_ENABLED)
    set(FISH_VK_SOURCES src/gpu/vk_loader.cpp src/gpu/vulkan_backend.cpp src/gpu/requant.cpp)
    list(APPEND FISH_CORE ${FISH_VK_SOURCES} ${FISH_VK_INC})
    # The SDK include dir carries unrelated headers; scope it to these files.
    set_source_files_properties(${FISH_VK_SOURCES} PROPERTIES
        INCLUDE_DIRECTORIES "${FISH_VK_INCLUDE_DIR};${FISH_VK_INC_DIR}"
        OBJECT_DEPENDS "${FISH_VK_INC}")
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
        # The thread pool uses std::thread; glibc < 2.34 (manylinux_2_28) keeps
        # pthread_create in libpthread, so link it explicitly (CI: undefined
        # reference to `pthread_create` when linking fish_server against libfish.so).
        find_package(Threads REQUIRED)
        target_link_libraries(fish PUBLIC Threads::Threads)
        if(FISH_VK_ENABLED)
            target_link_libraries(fish PRIVATE ${CMAKE_DL_LIBS})   # dlopen of libvulkan
        endif()
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

    foreach(name m1 m2 m5 gpu)
        if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/test/test_${name}.cpp)
            add_executable(fish_test_${name} test/test_${name}.cpp ${FISH_TEST_SOURCES})
            target_include_directories(fish_test_${name} PRIVATE include src)
            target_compile_definitions(fish_test_${name} PRIVATE FISH_HAVE_X86_KERNELS=${FISH_X86}
                                       FISH_HAVE_VULKAN=$<BOOL:${FISH_VK_ENABLED}>)
            if(MINGW)
                target_link_options(fish_test_${name} PRIVATE -static)
            else()
                target_link_libraries(fish_test_${name} PRIVATE Threads::Threads)   # glibc < 2.34
                if(FISH_VK_ENABLED)
                    target_link_libraries(fish_test_${name} PRIVATE ${CMAKE_DL_LIBS})
                endif()
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
