# Vendored fonts

All fonts are SIL Open Font License 1.1, vendored as `.woff2` so the lab
works offline with no CDN.

| File | Family | Use | Source |
|---|---|---|---|
| `inter.woff2` | Inter | UI and body text | Fontsource `@fontsource-variable/inter` |
| `manrope.woff2` | Manrope | alternative UI font | Fontsource `@fontsource-variable/manrope` |
| `space-grotesk.woff2` | Space Grotesk | alternative UI font | Fontsource `@fontsource-variable/space-grotesk` |
| `jetbrains-mono.woff2` | JetBrains Mono | code and mono text | Fontsource `@fontsource-variable/jetbrains-mono` |
| `OFL.txt` | - | license text | SIL OFL 1.1 |

The production build must ship `OFL.txt` next to these files.
