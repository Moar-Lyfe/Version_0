# Running it on the local network

The dashboard is designed to live on one machine on your LAN and be opened by
everyone else — desktop or phone — over `http://<that-machine>:8501`. Nothing
about it needs, or reaches, the internet.

---

## What "LAN only" already gives you

**The app makes no outbound requests.** Verified in a browser: loading every
page, switching every period and rendering the chart produced 156 requests, all
of them to the dashboard's own port. Streamlit serves its own JavaScript, the
chart runtime is bundled rather than pulled from a CDN, the theme uses system
fonts rather than Google Fonts, and `browser.gatherUsageStats = false` in
`.streamlit/config.toml` disables Streamlit's telemetry. Air-gap the machine and
the dashboard still works.

**The workbooks never leave the machine.** They are read from disk or a share,
aggregated in memory, and rendered. Nothing is uploaded, and `.gitignore`
excludes every `.xlsx`/`.xls`/`.csv` under `data/`, plus `config/config.yaml`,
so no local path or production figure can be committed by accident.

## What it does not give you

**Anyone who can reach the port can see everything.** Streamlit has no user
accounts. On a trusted LAN that is usually the intent — but it means the KPIs,
the agent-level breakdown and the CSV export are open to every device on the
network, guest Wi-Fi included if that shares a subnet.

**The Admin panel runs Python as the service user.** Anyone who can reach the
page can run the scripts in your scripts directory. That is the panel's whole
purpose, but it is worth being deliberate about:

```yaml
admin:
  password_env: "EXEC_DASH_ADMIN_PASSWORD"   # panel asks for this value
```

Set the variable in the service definition (see below) and the panel is gated;
leave `password_env: null` and it is open to the LAN. Setting
`admin.enabled: false` removes the panel entirely — worth doing if the pipeline
is only ever run from a terminal with `tools/run_pipeline.py`.

**Stack traces are visible.** `showErrorDetails = "full"` puts real tracebacks
on the page, which is what makes Diagnostics useful. Set it to `"none"` if the
network is less trusted than the room.

---

## Setting it up

### 1. Bind it to the LAN interface

`.streamlit/config.toml` ships with `address = "0.0.0.0"` — every interface,
which is what makes phone access work out of the box. On a machine with more
than one network card, name the LAN address explicitly instead, so the app can
never start listening somewhere you did not intend:

```toml
[server]
address = "192.168.1.50"    # this machine's LAN IP
port = 8501
```

Find the address with `ipconfig` (Windows), `ip addr` (Linux) or
`ipconfig getifaddr en0` (macOS). Give the machine a **DHCP reservation or a
static IP** first — otherwise the URL everyone bookmarked changes on the next
lease. Better still, add a DNS entry so people type `http://dashboard:8501`.

### 2. Open the port to the LAN only

**Windows** — allow 8501 inbound from your subnet, and nothing else:

```bat
netsh advfirewall firewall add rule name="Executive Dashboard" ^
  dir=in action=allow protocol=TCP localport=8501 ^
  remoteip=192.168.1.0/24
```

**Linux (ufw):**

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8501 proto tcp
```

**macOS** — the application firewall prompts on first launch; allow incoming
connections for the Python interpreter inside `.venv`.

Constraining `remoteip`/`from` to the subnet is the belt to the binding's
braces: even if the machine is later plugged into a different network, the rule
does not follow it.

### 3. Keep it off the internet

Do not port-forward 8501, and do not put it on a machine in a DMZ. If someone
needs it from outside, the answer is a VPN into the LAN — not an exposed port.
Leave `enableXsrfProtection` at its default (`true`); disabling it is only ever
needed for proxy setups you do not have.

### 4. Start it automatically

The dashboard should come back on its own after a reboot.

**Linux** — `deploy/executive-dashboard.service` is a ready systemd unit:

```bash
sudo cp deploy/executive-dashboard.service /etc/systemd/system/
sudo nano /etc/systemd/system/executive-dashboard.service   # edit the paths
sudo systemctl daemon-reload
sudo systemctl enable --now executive-dashboard
journalctl -u executive-dashboard -f
```

**Windows** — a scheduled task that runs at boot:

```bat
schtasks /create /tn "Executive Dashboard" /sc onstart /ru "DOMAIN\svc_reporting" ^
  /tr "C:\Reporting\Version_0\run.bat" /rl highest /f
```

> Run the task as a **real account that can read the workbook share**, not as
> `SYSTEM`. `SYSTEM` cannot see mapped drives, and it usually has no rights on a
> file server. For the same reason, always write shares as UNC paths in
> `config.yaml` (`//fileserver/reports/daily`), never as a mapped letter
> (`Z:/...`) — the letter only exists inside an interactive session.

**macOS** — a `launchd` plist in `~/Library/LaunchAgents/` with
`RunAtLoad` and `KeepAlive`, invoking `run.sh`.

Whatever you use, the working directory must be the project root: Streamlit
only reads `.streamlit/config.toml` relative to the working directory, and
launching from elsewhere silently loses the theme and the port.

### 5. Ignore the "External URL" in the startup banner

Streamlit prints three URLs when it starts:

```
Local URL:    http://localhost:8501
Network URL:  http://192.168.1.50:8501
External URL: http://203.0.113.9:8501
```

**"External URL" is not a claim that the app is reachable from the internet.**
Streamlit resolves the machine's outward-facing address and prints it as a
convenience; whether anything can actually connect depends entirely on your
firewall and router, and by default nothing outside the LAN can. Hand people the
**Network URL**. If seeing the line at all is unwelcome, binding to the LAN
address (step 1) makes Streamlit print only that one.

### 6. Open it from a phone

Same URL: `http://192.168.1.50:8501`, on Wi-Fi on the same network. In Safari or
Chrome, **Add to Home Screen** gives it an icon that opens without browser
chrome, which is what the mobile layout was built for.

---

## Scheduling the alert check

The Analytics rules can run without anyone opening the dashboard.
`tools/check_alerts.py` exits 0 when everything is on track, 1 on a warning, 2 on
a critical and 3 when it could not evaluate — so a scheduler can act on the
result.

**Linux** — a systemd timer, or cron:

```cron
# Weekday mornings, log the result and let the exit code speak.
0 7 * * 1-6  cd /opt/executive-dashboard && .venv/bin/python tools/check_alerts.py --quiet >> runtime/logs/alerts.log 2>&1
```

**Windows** — a second scheduled task:

```bat
schtasks /create /tn "Dashboard alert check" /sc daily /st 07:00 ^
  /ru "DOMAIN\svc_reporting" ^
  /tr "C:\Reporting\Version_0\.venv\Scripts\python.exe C:\Reporting\Version_0\tools\check_alerts.py --quiet"
```

Add `--json` or `--csv` if something downstream should consume the result. There
is no email step: this machine has no outbound access by design, so wire the
exit code into whatever monitoring already runs on your network rather than
giving the dashboard a mail path off the LAN.

## Checking it is healthy

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.50:8501/healthz   # 200
```

`/healthz` is Streamlit's own endpoint and needs no authentication — handy for a
monitoring check, and harmless to expose on the LAN since it returns nothing but
`ok`.

If the page loads but the numbers are empty, the problem is the data path, not
the network: open **Diagnostics**, which shows whether each configured source
resolved and how many files it matched. A service account that cannot read the
share is the usual culprit, and it shows up there as `Exists: NO`.
