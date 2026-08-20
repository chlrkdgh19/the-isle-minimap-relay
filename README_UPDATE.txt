The Isle MiniMap Relay Server v1.3 Update

Replace cloud_relay_server.py in your existing Render GitHub repository and commit.
Render should redeploy automatically.

After deployment open:
https://the-isle-minimap-relay.onrender.com/health

Confirm JSON contains:
"version": "1.3"

New server features:
- destination owner lock
- shared danger pings (45 sec TTL)
- automatic owner transfer
- existing shared destination / clear paths preserved
