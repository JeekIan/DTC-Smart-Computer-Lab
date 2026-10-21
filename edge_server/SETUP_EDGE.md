# Lab server (edge) setup

1. PC: always on, LAN cable, FIXED IP (router DHCP reservation), Python 3.10+ and MySQL/MariaDB.
2. Copy your project folder here, then paste the files from this zip over the same paths.
3. `pip install -r requirements.txt`
4. Create the MySQL database `compulab_db` (utf8mb4); user/password must match DATABASES in settings.py.
5. Open `settings_edge_snippet.py`, copy its values into `compulab/settings.py` (edit the IP, domain, secrets).
6. `python manage.py migrate` then `python manage.py createsuperuser`.
7. In Django admin: create Labs (SAME name/hours as the cloud), PCs (correct ip_address), and the
   Admin / In-Charge / Instructor accounts (instructor emails SAME as on the cloud).
8. Firewall (Command Prompt as Administrator):
   netsh advfirewall firewall add rule name="CompuLab 8000" dir=in action=allow protocol=TCP localport=8000 profile=any
9. Run `start_compulab.bat` (put it next to manage.py). To start it automatically: Task Scheduler ->
   Create Task -> Trigger "At startup" -> Action "Start a program" = start_compulab.bat
   (tick "Run whether user is logged on or not").
10. Test: from another PC open http://<lab server IP>:8000/ ; on the lab server open
    https://yourdomain.com/api/ping/ -> {"ok": true, "role": "cloud"}.
    In Django admin > Cloud sync > Sync queue, rows turn synced=True within a minute.

Lab PC agent_config.json:  "server_url": "http://<lab server IP>:8000"   (NOT the cloud).
