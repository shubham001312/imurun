# Hardware Probe — `hardware_probe.cpp`

## Purpose
**One-shot system interrogation at startup.** Returns `HardwareCaps` struct used by `LayerPlanner` to size the VRAM arena, staging ring, and choose quantization policy. Runs in < 50 ms.

## Output: `HardwareCaps`
```cpp
struct HardwareCaps {
    size_t ram_bytes = 0;           // Total physical RAM
    size_t vram_bytes = 0;          // Dedicated GPU VRAM (0 if no GPU)
    int cpu_cores = 0;              // Logical cores (hardware_concurrency)
    bool has_mmap = false;          // mmap / MapViewOfFile works
    bool has_direct_io = false;     // O_DIRECT / FILE_FLAG_NO_BUFFERING works
    DiskSpeed disk{};               // Seq read/write MB/s, random IOPS
    std::string gpu_name;           // e.g. "NVIDIA GeForce RTX 3050 Laptop GPU"
    std::string gpu_driver;         // Driver version string
};
```

## Detection Methods

### RAM
| OS | API |
|----|-----|
| Windows | `GlobalMemoryStatusEx` → `ullTotalPhys` |
| Linux | `sysinfo` → `totalram * mem_unit` |

### CPU Cores
- `std::thread::hardware_concurrency()` — logical cores
- Falls back to 1 if 0

### mmap / Direct I/O
| Feature | Windows | Linux |
|---------|---------|-------|
| mmap | Always available (`MapViewOfFile`) | Always available |
| Direct I/O | Windows 8+ (`FILE_FLAG_NO_BUFFERING`) | `O_DIRECT` (most filesystems) |
| Detection | `VerifyVersionInfoW` ≥ 6.2 | Compile-time `true` |

### GPU / VRAM (Windows only in current impl)
```cpp
// DXGI 1.1 enumeration
IDXGIFactory1* factory;
CreateDXGIFactory1(__uuidof(IDXGIFactory1), &factory);
for (UINT i = 0; factory->EnumAdapters1(i, &adapter) != DXGI_ERROR_NOT_FOUND; ++i) {
    DXGI_ADAPTER_DESC1 desc;
    adapter->GetDesc1(&desc);
    if (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) continue; // Skip WARP
    vram_bytes = desc.DedicatedVideoMemory;
    gpu_name = wide_to_utf8(desc.Description);
    break;
}
```
**Linux TODO**: Parse `/sys/class/drm/card*/device/mem_info_vram_total` or use `nvml`/`rocm_smi`.

### Disk Speed (Heuristic)
Current impl returns NVMe assumptions:
```cpp
disk.seq_read_mb_s  = 3000;
disk.seq_write_mb_s = 2500;
disk.random_read_iops = 300000;
```
**Real benchmark TODO**: 4 KB random read/write at startup (async, non-blocking).

## Connection to LayerPlanner
```cpp
HardwareCaps caps = probe_hardware();
TensorTable tensors = inspect_gguf(model_path);
LayerPlan plan = make_layer_plan(caps, tensors, user_vram_limit);
//         ▲
//         └─ uses: caps.vram_bytes, caps.ram_bytes, caps.disk.seq_read_mb_s
//            to decide hot/warm/cold split and staging ring size
```

## Invariants
1. `ram_bytes > 0` always
2. `cpu_cores ≥ 1`
3. `has_mmap == true` on all supported platforms
4. `vram_bytes == 0` ⇔ no discrete GPU detected
5. Function is **idempotent** — safe to call multiple times

## Testing
```cpp
TEST(HardwareProbe, Basic) {
    auto caps = probe_hardware();
    EXPECT_GT(caps.ram_bytes, 1ull * 1024 * 1024 * 1024); // > 1 GB
    EXPECT_GE(caps.cpu_cores, 1);
    EXPECT_TRUE(caps.has_mmap);
    // VRAM may be 0 on CPU-only machines
}
```

## Future Improvements
- Linux GPU detection (DRM, NVML, ROCm)
- Actual disk benchmark (fio-style, 4 KB random, 1 s)
- CPU frequency scaling detection (`cpufreq`)
- Thermal/throttling headroom estimate
- Container-aware cgroup limits (`/sys/fs/cgroup/memory.max`)