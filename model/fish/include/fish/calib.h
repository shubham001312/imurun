#pragma once
/*
 * FISH - load-time calibration for device re-quantisation (src/calib.cpp).
 *
 * Importance = per projection (by tensor name), E[x_j^2] of its input column
 * j over the built-in calibration text, measured on the CPU path at file
 * precision. The re-quantisers in src/gpu/requant.cpp weight their error by
 * it. Both the importance and the re-quantised device bytes are cached under
 * ~/.imurun/cache/fish/<model key>/ (FISH_CACHE_DIR overrides the root,
 * FISH_GPU_CACHE=0 disables the cache).
 */
#include <string>
#include <unordered_map>
#include <vector>

#include "gguf.h"

namespace fish {

using Importance = std::unordered_map<std::string, std::vector<float>>;

const char* calibration_text();

// Creates and returns the cache directory for this model file + variant
// (adapter, format version), or "" when caching is off or impossible.
std::string device_cache_dir(const GgufFile& g, const std::string& variant);
// "<tensor name>.<fmt>.dev" with unsafe characters replaced.
std::string cache_file_name(const std::string& tensor, const char* fmt);

bool importance_save(const std::string& path, const Importance& imp, int tokens);
bool importance_load(const std::string& path, Importance* imp, int* tokens);

} // namespace fish
