#include <fish/engine.h>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <vector>
#include <thread>

#ifdef _WIN32
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <psapi.h>
#include <dxgi.h>
#include <d3d11.h>
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "psapi.lib")
#else
#include <unistd.h>
#include <sys/sysinfo.h>
#include <fstream>
#include <dirent.h>
#endif

namespace fish {

HardwareCaps probe_hardware() {
    HardwareCaps caps;

    // ---- RAM ----
#ifdef _WIN32
    MEMORYSTATUSEX mem{};
    mem.dwLength = sizeof(mem);
    if (GlobalMemoryStatusEx(&mem)) {
        caps.ram_bytes = mem.ullTotalPhys;
    }
#else
    struct sysinfo si;
    if (sysinfo(&si) == 0) {
        caps.ram_bytes = static_cast<size_t>(si.totalram) * si.mem_unit;
    }
#endif

    // ---- CPU cores ----
    caps.cpu_cores = static_cast<int>(std::thread::hardware_concurrency());
    if (caps.cpu_cores <= 0) caps.cpu_cores = 1;

    // ---- mmap / direct I/O support ----
    // Conservative: assume mmap works on all desktop OSes
    caps.has_mmap = true;
#ifdef _WIN32
    // Windows 8+ has PrefetchVirtualMemory
    OSVERSIONINFOEXW osvi{};
    osvi.dwOSVersionInfoSize = sizeof(osvi);
    osvi.dwMajorVersion = 6;
    osvi.dwMinorVersion = 2; // Windows 8
    DWORDLONG cm = VerSetConditionMask(0, VER_MAJORVERSION, VER_GREATER_EQUAL);
    cm = VerSetConditionMask(cm, VER_MINORVERSION, VER_GREATER_EQUAL);
    caps.has_direct_io = VerifyVersionInfoW(&osvi, VER_MAJORVERSION | VER_MINORVERSION, cm) != FALSE;
#else
    // Linux: O_DIRECT works on most filesystems
    caps.has_direct_io = true;
#endif

    // ---- GPU (VRAM) ----
#ifdef _WIN32
    // Try DXGI to get dedicated VRAM
    IDXGIFactory1* factory = nullptr;
    if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&factory))) {
        IDXGIAdapter1* adapter = nullptr;
        for (UINT i = 0; factory->EnumAdapters1(i, &adapter) != DXGI_ERROR_NOT_FOUND; ++i) {
            DXGI_ADAPTER_DESC1 desc{};
            if (SUCCEEDED(adapter->GetDesc1(&desc))) {
                // Skip software adapter
                if (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) {
                    adapter->Release();
                    continue;
                }
                // Convert wide to narrow (simplified)
                char buf[128];
                WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1, buf, sizeof(buf), nullptr, nullptr);
                caps.gpu_name = buf;
                caps.vram_bytes = static_cast<size_t>(desc.DedicatedVideoMemory);
                adapter->Release();
                break; // First hardware adapter
            }
            adapter->Release();
        }
        factory->Release();
    }
#else
    // Linux: try /sys/class/drm/card*/device/mem_info_vram_total
    // Simplified: leave at 0 for now; user can override via env
#endif

    // ---- Disk speed (rough estimate) ----
    // In a real impl, run a quick benchmark; here we use heuristics
    caps.disk.seq_read_mb_s = 3000;  // NVMe assumption
    caps.disk.seq_write_mb_s = 2500;
    caps.disk.random_read_iops = 300000;

    return caps;
}

} // namespace fish