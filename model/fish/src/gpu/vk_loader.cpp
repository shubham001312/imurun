#include "vk_loader.h"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace fish {
namespace gpu {

namespace {

void* open_loader() {
#if defined(_WIN32)
    // Search the application directory and System32 only: never the current
    // directory or PATH, so a planted vulkan-1.dll next to a model file cannot
    // be picked up.
    HMODULE h = LoadLibraryExW(L"vulkan-1.dll", nullptr, LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!h) h = LoadLibraryExW(L"vulkan-1.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    return reinterpret_cast<void*>(h);
#elif defined(__APPLE__)
    const char* names[] = {"libvulkan.1.dylib", "libvulkan.dylib", "libMoltenVK.dylib"};
    for (const char* n : names)
        if (void* h = dlopen(n, RTLD_NOW | RTLD_LOCAL)) return h;
    return nullptr;
#else
    const char* names[] = {"libvulkan.so.1", "libvulkan.so"};
    for (const char* n : names)
        if (void* h = dlopen(n, RTLD_NOW | RTLD_LOCAL)) return h;
    return nullptr;
#endif
}

void* lib_symbol(void* lib, const char* name) {
#if defined(_WIN32)
    return reinterpret_cast<void*>(GetProcAddress(reinterpret_cast<HMODULE>(lib), name));
#else
    return dlsym(lib, name);
#endif
}

} // namespace

bool VkApi::load_library(const char** why) {
    if (vkGetInstanceProcAddr) return true;
    lib = open_loader();
    if (!lib) { if (why) *why = "no Vulkan loader library on this system"; return false; }
    vkGetInstanceProcAddr = reinterpret_cast<PFN_vkGetInstanceProcAddr>(lib_symbol(lib, "vkGetInstanceProcAddr"));
    if (!vkGetInstanceProcAddr) { if (why) *why = "Vulkan loader lacks vkGetInstanceProcAddr"; return false; }
#define FISH_VK_LOAD(fn) fn = reinterpret_cast<PFN_##fn>(vkGetInstanceProcAddr(VK_NULL_HANDLE, #fn));
    FISH_VK_GLOBAL_FNS(FISH_VK_LOAD)
#undef FISH_VK_LOAD
    // vkEnumerateInstanceVersion is absent on a Vulkan 1.0 loader; the caller
    // treats that as "too old" rather than as a failure to load.
    if (!vkCreateInstance || !vkEnumerateInstanceExtensionProperties) {
        if (why) *why = "Vulkan loader is missing global entry points";
        return false;
    }
    return true;
}

bool VkApi::load_instance(VkInstance inst, const char** why) {
#define FISH_VK_LOAD(fn) fn = reinterpret_cast<PFN_##fn>(vkGetInstanceProcAddr(inst, #fn)); \
    if (!fn) { if (why) *why = "Vulkan instance function missing: " #fn; return false; }
    FISH_VK_INSTANCE_FNS(FISH_VK_LOAD)
#undef FISH_VK_LOAD
    return true;
}

bool VkApi::load_device(VkDevice dev, const char** why) {
#define FISH_VK_LOAD(fn) fn = reinterpret_cast<PFN_##fn>(vkGetDeviceProcAddr(dev, #fn)); \
    if (!fn) { if (why) *why = "Vulkan device function missing: " #fn; return false; }
    FISH_VK_DEVICE_FNS(FISH_VK_LOAD)
#undef FISH_VK_LOAD
    return true;
}

} // namespace gpu
} // namespace fish
