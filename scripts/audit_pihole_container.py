import json
import sys


item = json.load(sys.stdin)[0]
labels = item.get("Config", {}).get("Labels") or {}
environment = {}
for entry in item.get("Config", {}).get("Env") or []:
    key, _, value = entry.partition("=")
    environment[key] = value
host = item.get("HostConfig") or {}
print("compose_workdir=" + str(labels.get("com.docker.compose.project.working_dir") or "unknown"))
print("compose_files=" + str(labels.get("com.docker.compose.project.config_files") or "unknown"))
print("admin_password_set=" + str(bool(environment.get("FTLCONF_webserver_api_password"))).lower())
upstreams = environment.get("FTLCONF_dns_upstreams", "")
print("upstream_count=" + str(len([value for value in upstreams.split(";") if value.strip()])))
print("listening_mode=" + str(environment.get("FTLCONF_dns_listeningMode") or "default"))
print("restart_policy=" + str((host.get("RestartPolicy") or {}).get("Name") or "none"))
print("security_opt=" + ",".join(host.get("SecurityOpt") or []))
print("cap_add_count=" + str(len(host.get("CapAdd") or [])))
print("device_count=" + str(len(host.get("Devices") or [])))
