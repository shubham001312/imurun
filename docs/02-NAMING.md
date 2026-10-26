# Naming

- IMURUN - the complete system: agent, UIs, backend, engine, everything.
- FISH - the core model-running module (model/): the architecture that runs
  a model on a local device at the highest speed the machine allows, with
  weights streamed from disk layer by layer instead of loaded whole.

FISH in one paragraph (design intent, to be validated by benchmark later):
weights live on the SSD and are memory-mapped; a resident window keeps the
hot layers in RAM, cold layers stream from disk with readahead, and the CPU
or GPU is chosen per capability. Hard limit accepted up front (V2): SSD
~3-7 GB/s vs RAM ~25-50 GB/s, so small models are fast and huge models run
at all on machines that could never hold them, but a 70B on 8 GB is SSD
bound no matter the language.

## Foundation (V24, 2026-10-26)

FISH is HYBRID: llama.cpp is the compute core (linked into the server
process, not a separate service), and FISH owns the speed layer on top -
the resident-window scheduler, mmap streaming policy, swap and stats.
Inference math is not written from scratch; the from-scratch path was
rejected in favour of reaching the speed goal sooner.
