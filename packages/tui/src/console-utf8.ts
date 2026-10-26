/**
 * Windows console code page — the reason em dashes become "ΓÇö".
 *
 * A Windows console speaks an OEM code page by default (CP850, CP437), so
 * UTF-8 output turns into mojibake: this app's own glyphs and, worse, the
 * model's words. Windows can switch a console to UTF-8 per process:
 * SetConsoleOutputCP(65001) for output and SetConsoleCP(65001) for input,
 * both from kernel32. Called once at startup, best-effort — a terminal that
 * already speaks UTF-8 (Windows Terminal) ignores it, and a failure costs
 * nothing.
 */
import { dlopen, FFIType } from "bun:ffi"

let done = false

export function enableUtf8Console(): void {
  if (done || process.platform !== "win32") return
  done = true
  try {
    const kernel32 = dlopen("kernel32.dll", {
      SetConsoleOutputCP: { args: [FFIType.uint32_t], returns: FFIType.int32_t },
      SetConsoleCP: { args: [FFIType.uint32_t], returns: FFIType.int32_t },
    })
    kernel32.symbols.SetConsoleOutputCP(65001)
    kernel32.symbols.SetConsoleCP(65001)
  } catch {
    // The console keeps its code page; the app still runs.
  }
}
