# First install: the gate and its watchdog

These two timers start promoter-bot. They are kept out of `systemd/` on purpose: the hourly sync pulse installs
everything directly under `systemd/` and overwrites a unit of the same name, and a host that already runs the gate may
have its own versions of these.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/first-install/churn-tick.* systemd/first-install/churn-watchdog.* ~/.config/systemd/user/
cp systemd/churn-poll.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now churn-tick.timer churn-watchdog.timer churn-poll.timer
loginctl enable-linger "$USER"     # so the timers run when you are not logged in
```

`churn status` shows whether the timer is active. `churn stop --hours 8 --reason "..."` pauses the gate without
removing anything.
