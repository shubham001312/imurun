#pragma once
/*
 * FISH Vulkan backend — runtime loader for the Vulkan API.
 *
 * fish.dll must load on machines without any Vulkan driver, so nothing links
 * vulkan-1.lib / libvulkan.so. The loader library is opened at runtime
 * (LoadLibraryExW / dlopen) and every entry point is fetched through
 * vkGetInstanceProcAddr / vkGetDeviceProcAddr into the table below (the same
 * idea as volk, without the dependency). The library handle is never closed:
 * unloading a Vulkan driver during process exit is a classic source of
 * crashes at DLL detach.
 */
#ifndef VK_NO_PROTOTYPES
#define VK_NO_PROTOTYPES
#endif
#include <vulkan/vulkan.h>

namespace fish {
namespace gpu {

#define FISH_VK_GLOBAL_FNS(X)                     \
    X(vkCreateInstance)                           \
    X(vkEnumerateInstanceVersion)                 \
    X(vkEnumerateInstanceExtensionProperties)     \
    X(vkEnumerateInstanceLayerProperties)

#define FISH_VK_INSTANCE_FNS(X)                   \
    X(vkDestroyInstance)                          \
    X(vkEnumeratePhysicalDevices)                 \
    X(vkGetPhysicalDeviceProperties)              \
    X(vkGetPhysicalDeviceQueueFamilyProperties)   \
    X(vkGetPhysicalDeviceMemoryProperties)        \
    X(vkGetPhysicalDeviceMemoryProperties2)       \
    X(vkEnumerateDeviceExtensionProperties)       \
    X(vkCreateDevice)                             \
    X(vkGetDeviceProcAddr)

#define FISH_VK_DEVICE_FNS(X)                     \
    X(vkDestroyDevice)                            \
    X(vkGetDeviceQueue)                           \
    X(vkDeviceWaitIdle)                           \
    X(vkCreateBuffer)                             \
    X(vkDestroyBuffer)                            \
    X(vkGetBufferMemoryRequirements)              \
    X(vkAllocateMemory)                           \
    X(vkFreeMemory)                               \
    X(vkBindBufferMemory)                         \
    X(vkMapMemory)                                \
    X(vkUnmapMemory)                              \
    X(vkInvalidateMappedMemoryRanges)             \
    X(vkCreateShaderModule)                       \
    X(vkDestroyShaderModule)                      \
    X(vkCreateDescriptorSetLayout)                \
    X(vkDestroyDescriptorSetLayout)               \
    X(vkCreatePipelineLayout)                     \
    X(vkDestroyPipelineLayout)                    \
    X(vkCreateComputePipelines)                   \
    X(vkDestroyPipeline)                          \
    X(vkCreateDescriptorPool)                     \
    X(vkDestroyDescriptorPool)                    \
    X(vkAllocateDescriptorSets)                   \
    X(vkFreeDescriptorSets)                       \
    X(vkUpdateDescriptorSets)                     \
    X(vkCreateCommandPool)                        \
    X(vkDestroyCommandPool)                       \
    X(vkAllocateCommandBuffers)                   \
    X(vkResetCommandBuffer)                       \
    X(vkBeginCommandBuffer)                       \
    X(vkEndCommandBuffer)                         \
    X(vkCmdCopyBuffer)                            \
    X(vkCmdPipelineBarrier)                       \
    X(vkCmdBindPipeline)                          \
    X(vkCmdBindDescriptorSets)                    \
    X(vkCmdPushConstants)                         \
    X(vkCmdDispatch)                              \
    X(vkCreateFence)                              \
    X(vkDestroyFence)                             \
    X(vkResetFences)                              \
    X(vkWaitForFences)                            \
    X(vkQueueSubmit)                              \
    X(vkCreateQueryPool)                          \
    X(vkDestroyQueryPool)                         \
    X(vkCmdResetQueryPool)                        \
    X(vkCmdWriteTimestamp)                        \
    X(vkGetQueryPoolResults)

struct VkApi {
    void* lib = nullptr;
    PFN_vkGetInstanceProcAddr vkGetInstanceProcAddr = nullptr;
#define FISH_VK_DECLARE(fn) PFN_##fn fn = nullptr;
    FISH_VK_GLOBAL_FNS(FISH_VK_DECLARE)
    FISH_VK_INSTANCE_FNS(FISH_VK_DECLARE)
    FISH_VK_DEVICE_FNS(FISH_VK_DECLARE)
#undef FISH_VK_DECLARE

    // Open the platform's Vulkan loader and fetch the global entry points.
    // Returns false (and leaves everything null) when there is no loader.
    bool load_library(const char** why);
    // Fetch instance / device entry points. False if any required one is missing.
    bool load_instance(VkInstance inst, const char** why);
    bool load_device(VkDevice dev, const char** why);
};

} // namespace gpu
} // namespace fish
