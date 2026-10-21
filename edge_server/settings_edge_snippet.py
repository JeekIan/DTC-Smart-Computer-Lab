# ==== Add/replace these in compulab/settings.py on the LAB SERVER (edge) ====
ALLOWED_HOSTS = ['192.168.1.162', 'localhost', '127.0.0.1']   # <- the lab server's own LAN IP

CLOUD_SYNC_ROLE = 'edge'
CLOUD_SERVER_URL = 'https://yourdomain.com'          # <- your cPanel domain (no trailing slash)
CLOUD_SYNC_API_KEY = 'DTC-SmartComputerLaboratoryManagementSystem'   # <- MUST be identical on the cloud
CLOUD_SYNC_INTERVAL_SECONDS = 60
CLOUD_SYNC_TIMEOUT_SECONDS = 5

# Also change this to a long random string and use the same value in every lab PC's agent_config.json ("secret"):
# PC_AGENT_SHARED_SECRET = '...'
