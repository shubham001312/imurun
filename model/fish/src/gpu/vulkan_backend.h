#pragma once
/*
 * FISH Vulkan backend — the only symbols other code may use directly.
 *
 * The backend itself is reached through fish::gpu_backend() (backend.h); it
 * registers itself from a static initializer in vulkan_backend.cpp. These
 * helpers exist for tests and diagnostics.
 */
#include <string>

namespace fish {
namespace gpu {

// One line for stats: device, resident bytes by (GGUF type -> device type),
// span state. E.g. "vulkan NVIDIA ...: 2950 MB weights, 160 x Q4_K->FQ3 2100 MB
// (from 2700), 65 x Q6_K->Q4_K 850 MB (from 1240); span 32 layers, KV 64 MB f16".
std::string vulkan_describe();

// Human-readable state: "not initialised", "ready: <device>", or why the
// backend switched itself off ("no Vulkan loader library on this system", ...).
const char* vulkan_status();

// Force initialisation now (normally it happens on the first query from the
// model loader). Returns true when a usable device was opened.
bool vulkan_init_now();

// GPU-side kernel time (timestamp queries around the dispatches), enabled by
// FISH_GPU_PROFILE=1. Reports totals since the last reset; false when off.
bool vulkan_profile(double* kernel_ms, double* wall_ms, long* submits, bool reset);

} // namespace gpu
} // namespace fish
