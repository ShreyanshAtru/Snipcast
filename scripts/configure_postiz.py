"""Configure a NEW upstream checkout only. Never rotate secrets on an existing DB."""
from pathlib import Path
import re,secrets
p=Path('infra/postiz/upstream/docker-compose.yaml')
text=p.read_text()
text=text.replace('random string that is unique to every install - just type random characters here!',secrets.token_hex(48))
text=text.replace('postiz-password',secrets.token_hex(24))
# Remove fixed container/network names so this stack is isolated by Compose project.
text=re.sub(r'^    container_name:.*\n','',text,flags=re.M)
text=text.replace('    name: temporal-network\n','')
text=text.replace('"4007:5000"','"127.0.0.1:4007:5000"')
# This instance serves a local batch processor. API calls are also paced by Clipdesk.
text=text.replace('API_LIMIT: 30','API_LIMIT: 1000')
p.write_text(text); p.chmod(0o600)
