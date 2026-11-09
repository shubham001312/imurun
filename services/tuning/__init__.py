"""The automated local fine-tuning channel (see docs/tuning/README.md).

The app process only orchestrates: consent, data, scheduling, events, model
registration. All training happens in a separate venv
(``~/.imurun/tuning/venv``) installed by ``python -m services.tuning.setup``;
without it the channel reports "training stack not installed" and does nothing.
"""
