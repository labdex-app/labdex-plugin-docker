"""Docker: containers as hosts on the Docker host, their published ports and Traefik labels as
services, and their state as host status. Reads GET /containers/json (Engine API, any version)."""
from __future__ import annotations

import re
from fnmatch import fnmatchcase
from urllib.parse import urlparse

from labdex.plugins import servicesync
from labdex.plugins.base import Change, Plugin as Base, PluginError

ROUTER_RULE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")


class Plugin(Base):
    @property
    def api(self):
        headers = {"Authorization": f"Bearer {self.settings['token']}"} if self.settings.get("token") else None
        return self.ctx.http(self.settings["url"].rstrip("/"), headers=headers)

    def test(self):
        v = self.api.get("version").json()
        return f"Docker {v.get('Version', '?')} (API {v.get('ApiVersion', '?')}): {len(self.containers())} containers"

    def run_test(self, params):
        return {"message": self.test()}

    # ------------------------------------------------------------------ read
    def docker_host(self) -> str:
        return self.settings.get("host") or self.ctx.match_host()(urlparse(self.settings["url"]).hostname or "") or ""

    def containers(self) -> list[dict]:
        rows = self.api.get("containers/json", params={"all": "true"}).json()
        if not isinstance(rows, list):
            raise PluginError("unexpected answer from containers/json")
        ignore = self.settings.get("ignore") or []
        out = []
        for c in rows:
            name = ((c.get("Names") or ["/?"])[0]).lstrip("/")
            labels = c.get("Labels") or {}
            if any(fnmatchcase(name, p) for p in ignore):
                continue
            running = c.get("State") == "running"
            if not running and not self.settings.get("include_stopped"):
                continue
            nets = {}
            for net, n in ((c.get("NetworkSettings") or {}).get("Networks") or {}).items():
                nets[net] = {"ip4": n.get("IPAddress") or "", "ip6": n.get("GlobalIPv6Address") or "", "mac": (n.get("MacAddress") or "").lower()}
            out.append({"id": c.get("Id", "")[:12], "name": name, "image": c.get("Image", ""), "state": c.get("State", ""),
                        "status": c.get("Status", ""), "running": running, "labels": labels,
                        "service": labels.get("com.docker.compose.service") or name,
                        "project": labels.get("com.docker.compose.project", ""), "ports": c.get("Ports") or [], "networks": nets})
        return out

    def _links(self) -> dict[str, str]:
        """container name → host name, from this instance's host settings."""
        from labdex.models import HostModule, db
        return {(hm.settings or {}).get("container"): hm.host_name
                for hm in db.session.scalars(db.select(HostModule).where(HostModule.integration_id == self.ctx.integration.id))
                if (hm.settings or {}).get("container")}

    # ------------------------------------------------------------- services
    def _services(self, c: dict, on_container: bool) -> list[dict]:
        """Wanted services of a container: published ports (on the Docker host's addresses)
        and Traefik routers (with their host names as links)."""
        out, seen = [], set()
        if self.settings.get("traefik_labels", True):
            labels = c["labels"]
            port_label = next((v for k, v in sorted(labels.items()) if k.startswith("traefik.http.services.") and k.endswith(".loadbalancer.server.port")), "")
            private = int(port_label) if str(port_label).isdigit() else next((p.get("PrivatePort") for p in c["ports"] if p.get("PrivatePort")), None)
            for k, rule in sorted(labels.items()):
                m = ROUTER_RULE.match(k)
                if not m:
                    continue
                names = servicesync.rule_hosts(rule)
                if not names:
                    continue
                r = m.group(1)
                eps = {e.strip().lower() for e in labels.get(f"traefik.http.routers.{r}.entrypoints", "").split(",") if e.strip()}
                tls = labels.get(f"traefik.http.routers.{r}.tls", "").lower() == "true" or bool(eps & servicesync.TLS_ENTRYPOINTS) \
                    or any(k2.startswith(f"traefik.http.routers.{r}.tls.") for k2 in labels)
                published = next((p.get("PublicPort") for p in c["ports"] if p.get("PrivatePort") == private and p.get("PublicPort")), None)
                port = (private if on_container else published) or private
                out.append({"name": servicesync.clean_name(r if r != c["service"] else c["service"]), "port": port, "proto": "tcp",
                            "scheme": "https" if tls else "http", "dns_name": names[0] + ".", "path": servicesync.rule_path(rule),
                            "description": f"{c['name']} via Traefik"})
                seen.add(private)
        if self.settings.get("published_ports", True):
            pub = sorted({(p["PublicPort"], p.get("Type", "tcp"), p.get("PrivatePort")) for p in c["ports"] if p.get("PublicPort")})
            for public, proto, private in pub:
                if private in seen and on_container:
                    continue  # the Traefik service already stands for this port on the container's own address
                name = servicesync.clean_name(c["service"] if len(pub) == 1 else f"{c['service']}-{public}")
                out.append({"name": name, "port": private if on_container else public, "proto": proto,
                            "description": f"{c['name']} ({c['image']}), published {public}/{proto}"})
        return out

    # ---------------------------------------------------------------- import
    def _payload(self, c: dict, dhost) -> dict:
        from labdex import hostnames, naming
        from labdex.discovery import network_of as _network_of
        role = re.sub(r"[^a-z0-9]", "", c["service"].lower())[:20] or "app"
        p = {"name": hostnames.next_name(self.settings.get("category") or "srv", "ct", role)["name"],
             "status": "active" if c["running"] else "offline", "function": c["image"][:200], "runs_on": dhost["name"] if dhost else "",
             "tags": ["docker"] + ([c["project"].lower()[:32]] if c["project"] and re.match(r"^[a-z0-9][a-z0-9_.:-]{0,31}$", c["project"].lower()) else []),
             "notes": f"Docker container {c['name']} ({c['id']})"}
        fun = re.sub(r"[^a-z0-9-]", "-", c["name"].lower()).strip("-")
        if naming.FUN_RE.match(fun or ""):
            p["fun_names"] = [fun]
        ifaces = []
        for net, n in c["networks"].items():  # macvlan / ipvlan: addresses in an inventory network
            inv = _network_of(n["ip4"]) if n["ip4"] else None
            if inv is not None:
                ifaces.append({"network": inv.name, "label": net[:32], "ip4": n["ip4"], "mac": n["mac"], "kind": "virtual",
                               "parent": ""})
        p["interfaces"] = ifaces
        if not ifaces and dhost:
            p["zone"], p["zone_pinned"] = dhost["zone"], True
        return p

    def plan_import_containers(self, params):
        hosts = {h["name"]: h for h in self.ctx.inventory.hosts(include_offline=True)}
        dname = self.docker_host()
        dhost = hosts.get(dname)
        if dhost is None:
            raise PluginError("set the Docker host (an inventory host) in the settings, or point the API address at one")
        links = self._links()
        changes, wanted, planned = [], {}, set()
        by_name = {}
        for h in hosts.values():
            for n in [h["name"], *(h.get("fun_names") or [])]:
                by_name[n] = h["name"]
        for c in self.containers():
            if not self.settings.get("containers_as_hosts", True):
                wanted.setdefault(dname, []).extend(self._services(c, on_container=False))
                continue
            existing = links.get(c["name"]) or by_name.get(re.sub(r"[^a-z0-9-]", "-", c["name"].lower()).strip("-"))
            if existing and existing != dname:
                if existing not in links.values():
                    changes.append(Change("update", "link", existing, f"is container {c['name']} on {dname}",
                                          data={"host": existing, "container": c["name"]}))
                on_container = bool(hosts[existing]["interfaces"]) if existing in hosts else False
                wanted.setdefault(existing, []).extend(self._services(c, on_container=True))
                if not on_container and self.settings.get("published_ports", True):
                    wanted.setdefault(dname, []).extend(s for s in self._services(c, on_container=False) if not s.get("dns_name"))
                continue
            p = self._payload(c, dhost)
            while p["name"] in planned or p["name"] in hosts:
                base, nn = p["name"].rsplit("-", 1)
                p["name"] = f"{base}-{int(nn) + 1:02d}"
            planned.add(p["name"])
            on_container = bool(p["interfaces"])
            svcs = self._services(c, on_container=True)
            p["services"] = [s for s in svcs if on_container or s.get("dns_name")]  # links on the container's host
            if not on_container:  # published ports live on the Docker host's addresses
                wanted.setdefault(dname, []).extend(s for s in self._services(c, on_container=False) if not s.get("dns_name"))
            changes.append(Change("create", "host", p["name"], f"container {c['name']} ({c['image']}) on {dname} · "
                                  f"{len(p['services'])} service(s)", None, p, {"container": c["name"]}))
        changes += servicesync.plan(self.ctx, wanted, f"docker {self.ctx.name}")
        return changes

    def apply_import_containers(self, changes, params):
        from labdex import audit, store
        from labdex.models import HostModule, db
        made = linked = 0
        for c in changes:
            if c.kind not in ("host", "link"):
                continue
            if c.kind == "host":
                try:
                    h = store.save_host(dict(c.after))
                except store.ValidationError as exc:
                    db.session.rollback()
                    self.log(f"{c.name}: not imported: " + "; ".join(f"{e['field']}: {e['message']}" for e in exc.errors))
                    continue
                db.session.flush()
                audit.record("create", "host", h.name, None, store.host_to_dict(h), summary=f"imported from Docker ({c.data['container']})",
                             actor=(f"docker {self.ctx.name}", "system"))
                name, made = h.name, made + 1
            else:
                name, linked = c.data["host"], linked + 1
            hm = db.session.scalar(db.select(HostModule).where(HostModule.integration_id == self.ctx.integration.id,
                                                               HostModule.host_name == name))
            if hm is None:
                hm = HostModule(integration_id=self.ctx.integration.id, host_name=name, enabled=None, settings={})
                db.session.add(hm)
            hm.settings = {**(hm.settings or {}), "container": c.data["container"]}
            db.session.commit()
            self.log(f"{'imported' if c.kind == 'host' else 'linked'} {name}")
        res = servicesync.apply(self.ctx, [c for c in changes if c.kind == "service"], f"docker {self.ctx.name}")
        if made:
            from labdex.plugins import jobs
            jobs.emit("host.created")
        return {"imported": made, "linked": linked, "services": res["applied"]}

    # ---------------------------------------------------------------- status
    def run_poll(self, params):
        links = self._links()
        state = {c["name"]: c for c in self.containers()} if links else {}
        res = {}
        for cname, hname in links.items():
            c = state.get(cname)
            res[hname] = {"state": "up" if c and c["running"] else "down",
                          "detail": f"{cname}: {c['status']}" if c else f"{cname}: not there (or stopped)"}
        out = self.ctx.report_status(res)
        return {"message": f"{len(res)} container host(s), {sum(1 for r in res.values() if r['state'] == 'down')} down",
                "changes": len(out["changes"])}

    # ------------------------------------------------------------ page, widget
    def page_containers(self, args):
        try:
            cs = self.containers()
        except PluginError as exc:
            return {"summary": [], "sections": [], "note": f"Couldn't read Docker: {exc}"}
        links = self._links()
        rows = [{"name": c["name"], "image": c["image"], "state": c["status"] or c["state"],
                 "ports": ", ".join(f"{p['PublicPort']}→{p.get('PrivatePort')}/{p.get('Type', 'tcp')}" for p in c["ports"] if p.get("PublicPort")) or "—",
                 "traefik": ", ".join(h for k, v in c["labels"].items() if ROUTER_RULE.match(k) for h in servicesync.rule_hosts(v)) or "—",
                 "host": links.get(c["name"], ""), "_tone": "ok" if c["running"] else "warn"} for c in cs]
        return {"summary": [{"label": "containers", "value": len(rows), "tone": ""},
                            {"label": "running", "value": sum(1 for c in cs if c["running"]), "tone": "ok"},
                            {"label": "in the inventory", "value": sum(1 for r in rows if r["host"]), "tone": ""}],
                "sections": [{"title": f"Containers on {self.docker_host() or '?'}", "type": "table",
                              "columns": [{"key": "name", "label": "Container"}, {"key": "image", "label": "Image"},
                                          {"key": "state", "label": "State"}, {"key": "ports", "label": "Published"},
                                          {"key": "traefik", "label": "Traefik"}, {"key": "host", "label": "Host", "link": "host"}],
                              "rows": rows, "empty": "No containers."}],
                "note": "“Import containers” plans the missing hosts and services."}

    def widget_containers(self, host):
        cs = self.containers()
        stopped = [c for c in cs if not c["running"]]
        return {"kind": "stat", "value": sum(1 for c in cs if c["running"]), "unit": "running", "tone": "warn" if stopped else "ok",
                "sub": f"{len(stopped)} stopped" if stopped else f"on {self.docker_host() or '?'}"}
