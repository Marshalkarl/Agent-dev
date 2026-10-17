#!/usr/bin/env python3
"""devagent v2 : agent IA pour développeurs.

Commandes :
  review    relit les changements (bugs, sécurité, qualité)      [--staged]
  commit    propose un message de commit et commite après accord
  docs      met à jour la documentation après confirmation
  test      lance les tests et diagnostique les échecs
  fix       lance les tests et corrige les échecs (avec aperçu du diff)
  pr        rédige le titre et la description d'une pull request
  explain   explique un fichier ou dossier                       <cible>
  hook      installe un hook pre-commit de relecture automatique
  chat      discussion libre sur le dépôt
"""
import argparse
import difflib
import json
import os
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "2.1.0"

# Fournisseurs gratuits compatibles avec l'API « OpenAI chat completions ».
# Les offres gratuites et les noms de modèles changent : au besoin, utilisez --model
# ou DEVAGENT_MODEL, ainsi que DEVAGENT_BASE_URL pour un autre fournisseur.
PROVIDERS = {
    "groq": {"url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "model": "openai/gpt-oss-120b"},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash"},
    "openrouter": {"url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                   "model": "meta-llama/llama-3.3-70b-instruct:free"},
    "ollama": {"url": "http://localhost:11434/v1", "key_env": None, "model": "llama3.1:8b"},
}
PROVIDER = os.getenv("DEVAGENT_PROVIDER", "groq").lower()
BASE_URL = ""
API_KEY = ""
MODEL = ""
MAX_STEPS = 20          # garde-fou contre les boucles infinies
MAX_OUT = int(os.getenv("DEVAGENT_MAX_OUT", "20000"))  # taille max d'un résultat d'outil (caractères)
MAX_PREVIEW = 80        # lignes max d'un aperçu de diff
SENSITIVE_NAMES = {"id_rsa", "id_ed25519"}
SENSITIVE_EXT = {".pem", ".key"}

os.system("")  # active les couleurs ANSI sous Windows


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


SYSTEM = """Tu es devagent, un développeur senior qui assiste dans un dépôt Git.
Règles :
- Observe avant de conclure : git_status, get_diff, list_files, search_code, read_file.
- Cite toujours fichiers et lignes. Classe les problèmes par gravité (critique, important, mineur).
- Ne devine pas : si tu as besoin d'un fichier, lis-le.
- Pour modifier du code, préfère edit_file (changement ciblé) à write_file (fichier entier).
  Fais des modifications minimales, sans reformater ce qui n'est pas concerné.
- Les actions qui modifient quelque chose (edit_file, write_file, run_tests, git_commit)
  demandent une confirmation humaine : explique brièvement pourquoi tu les proposes.
- Messages de commit au format Conventional Commits (feat:, fix:, docs:, refactor:, test:, chore:).
- Réponds en français, de façon concise."""

PROMPTS = {
    "review": "Relis mes changements en cours. Signale bugs probables, failles de sécurité, "
              "problèmes de performance et manques de tests. Termine par un verdict "
              "(OK / à corriger).",
    "commit": "Examine les changements indexés (staged) et propose un message de commit "
              "Conventional Commits. Puis appelle git_commit avec ce message.",
    "docs": "Compare le code modifié et la documentation existante (README, docstrings). "
            "Mets à jour ce qui est obsolète ou manquant avec edit_file.",
    "test": "Lance les tests. S'ils échouent, lis le code concerné et explique la cause "
            "probable ainsi que le correctif à appliquer, sans modifier de fichier.",
    "fix": "Lance les tests. S'ils échouent, localise la cause, corrige avec edit_file "
           "(modifications minimales), puis relance les tests pour vérifier. "
           "Maximum 3 cycles ; si tu n'y arrives pas, explique pourquoi.",
    "pr": "Compare la branche courante à la branche principale avec branch_diff. Rédige "
          "un titre et une description de pull request : contexte, changements, risques, "
          "comment tester.",
}


def find_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip() or ".").resolve()


ROOT = find_root()


# --------------------------------------------------------------------------- utilitaires
def truncate(text: str) -> str:
    if len(text) > MAX_OUT:
        return text[:MAX_OUT] + f"\n[... tronqué, {len(text) - MAX_OUT} caractères en plus]"
    return text or "(aucune sortie)"


def sh(cmd: list[str], timeout: int = 120) -> str:
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"Erreur : délai dépassé ({timeout}s)"
    except FileNotFoundError:
        return f"Erreur : commande introuvable ({cmd[0]})"
    return truncate((r.stdout + r.stderr).strip())


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():  # hook, pipe, CI : jamais d'action sans humain
        return False
    return input(color(f"\n⚠  {question} [o/N] ", "33")).strip().lower() in {"o", "oui", "y", "yes"}


def safe_path(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError("chemin hors du dépôt refusé")
    if ".git" in p.relative_to(ROOT).parts:
        raise ValueError("accès au dossier .git refusé")
    is_env = p.name == ".env" or (p.name.startswith(".env.") and not p.name.endswith(".example"))
    if is_env or p.name in SENSITIVE_NAMES or p.suffix in SENSITIVE_EXT:
        raise ValueError("fichier sensible (secrets) : accès refusé")
    return p


def show_diff(old: str, new: str, name: str) -> None:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", lineterm="", n=2))
    for line in lines[:MAX_PREVIEW]:
        if line.startswith(("+++", "---")):
            print(color(line, "1"))
        elif line.startswith("+"):
            print(color(line, "32"))
        elif line.startswith("-"):
            print(color(line, "31"))
        elif line.startswith("@@"):
            print(color(line, "36"))
        else:
            print(line)
    if len(lines) > MAX_PREVIEW:
        print(color(f"... {len(lines) - MAX_PREVIEW} lignes de diff en plus", "90"))


def project_instructions() -> str:
    f = ROOT / "DEVAGENT.md"
    return f.read_text(encoding="utf-8", errors="replace")[:5000] if f.exists() else ""


def system_text() -> str:
    text = SYSTEM
    extra = project_instructions()
    if extra:
        text += "\n\nConsignes spécifiques au projet (DEVAGENT.md) :\n" + extra
    return text


# --------------------------------------------------------------------------- outils
def git_status() -> str:
    return sh(["git", "status", "--short"])


def get_diff(staged: bool = False) -> str:
    return sh(["git", "diff", "--staged"] if staged else ["git", "diff", "HEAD"])


def branch_diff(base: str = "") -> str:
    if not base:
        for cand in ("main", "master", "develop"):
            if subprocess.run(["git", "rev-parse", "--verify", "-q", cand],
                              cwd=ROOT, capture_output=True).returncode == 0:
                base = cand
                break
        else:
            return "Branche principale introuvable : préciser `base`."
    return sh(["git", "diff", f"{base}...HEAD"])


def list_files() -> str:
    return sh(["git", "ls-files"])


def read_file(path: str) -> str:
    return truncate(safe_path(path).read_text(encoding="utf-8", errors="replace"))


def search_code(pattern: str) -> str:
    return sh(["git", "grep", "-n", "-I", "-e", pattern])


def edit_file(path: str, old_str: str, new_str: str) -> str:
    p = safe_path(path)
    old = p.read_text(encoding="utf-8")
    n = old.count(old_str)
    if n != 1:
        return f"Erreur : old_str trouvé {n} fois (doit apparaître exactement une fois)."
    new = old.replace(old_str, new_str)
    show_diff(old, new, path)
    if not confirm(f"Appliquer cette modification à {path} ?"):
        return "Refusé par l'utilisateur."
    p.write_text(new, encoding="utf-8")
    return f"{path} modifié."


def write_file(path: str, content: str) -> str:
    p = safe_path(path)
    if p.exists():
        show_diff(p.read_text(encoding="utf-8", errors="replace"), content, path)
        question = f"Écraser {path} ?"
    else:
        print(color(f"+ nouveau fichier {path} ({len(content.splitlines())} lignes)", "32"))
        question = f"Créer {path} ?"
    if not confirm(question):
        return "Refusé par l'utilisateur."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"{path} écrit."


def detect_test_cmd() -> str | None:
    if os.getenv("DEVAGENT_TEST_CMD"):
        return os.environ["DEVAGENT_TEST_CMD"]
    if (ROOT / "artisan").exists():
        return "php artisan test"
    if (ROOT / "package.json").exists():
        return "npm test"
    if any((ROOT / f).exists() for f in ("pytest.ini", "pyproject.toml", "tests")):
        return "pytest -q"
    return None


def run_tests() -> str:
    cmd = detect_test_cmd()
    if not cmd:
        return "Aucune commande de tests détectée (définir DEVAGENT_TEST_CMD)."
    if not confirm(f"Lancer les tests : {cmd} ?"):
        return "Refusé par l'utilisateur."
    return sh(shlex.split(cmd), timeout=600)


def git_commit(message: str) -> str:
    staged = subprocess.run(["git", "diff", "--staged", "--name-only"],
                            cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not staged:
        return "Rien n'est indexé : faire d'abord `git add`."
    if not confirm(f"Commiter avec le message : « {message} » ?"):
        return "Refusé par l'utilisateur."
    return sh(["git", "commit", "-m", message])


FUNCS = {f.__name__: f for f in (
    git_status, get_diff, branch_diff, list_files, read_file, search_code,
    edit_file, write_file, run_tests, git_commit)}
READONLY = {"git_status", "get_diff", "branch_diff", "list_files", "read_file", "search_code"}


def tool(name: str, description: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


TOOLS = [
    tool("git_status", "Liste les fichiers modifiés, indexés ou non suivis."),
    tool("get_diff", "Retourne le diff Git.",
         {"staged": {"type": "boolean", "description": "true = seulement les changements indexés"}}),
    tool("branch_diff", "Diff entre la branche principale et la branche courante (pour une PR).",
         {"base": {"type": "string", "description": "branche de base (auto-détectée si vide)"}}),
    tool("list_files", "Liste les fichiers suivis par Git."),
    tool("read_file", "Lit un fichier du dépôt.", {"path": {"type": "string"}}, ["path"]),
    tool("search_code", "Cherche un motif dans le code (git grep).", {"pattern": {"type": "string"}}, ["pattern"]),
    tool("edit_file", "Remplace un extrait unique d'un fichier (aperçu du diff + confirmation humaine).",
         {"path": {"type": "string"},
          "old_str": {"type": "string", "description": "texte exact à remplacer, unique dans le fichier"},
          "new_str": {"type": "string"}}, ["path", "old_str", "new_str"]),
    tool("write_file", "Crée ou écrase un fichier entier (confirmation humaine requise).",
         {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    tool("run_tests", "Lance la suite de tests (confirmation humaine requise)."),
    tool("git_commit", "Commite les changements indexés (confirmation humaine requise).",
         {"message": {"type": "string"}}, ["message"]),
]

USAGE = {"input": 0, "output": 0}


class ApiError(Exception):
    pass


def configure(model: str | None = None) -> None:
    """Résout fournisseur, URL, modèle et clé API (variables d'environnement + options)."""
    global BASE_URL, API_KEY, MODEL
    cfg = PROVIDERS.get(PROVIDER, {})
    BASE_URL = os.getenv("DEVAGENT_BASE_URL") or cfg.get("url", "")
    if not BASE_URL:
        sys.exit(f"Fournisseur inconnu « {PROVIDER} » : choisissez {', '.join(PROVIDERS)} "
                 "ou définissez DEVAGENT_BASE_URL.")
    MODEL = model or os.getenv("DEVAGENT_MODEL") or cfg.get("model", "")
    if not MODEL:
        sys.exit("Modèle non défini : utilisez --model ou DEVAGENT_MODEL.")
    local = any(h in BASE_URL for h in ("localhost", "127.0.0.1"))
    key_env = cfg.get("key_env")
    API_KEY = (os.getenv("DEVAGENT_API_KEY") or (os.getenv(key_env) if key_env else "")
               or ("none" if local else ""))
    if not API_KEY:
        sys.exit(f"Définissez {key_env or 'DEVAGENT_API_KEY'} (voir README).")


def to_openai_tools(tools: list) -> list:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["input_schema"]}} for t in tools]


def build_body(messages: list, tools: list) -> dict:
    body = {"model": MODEL, "max_tokens": 4000, "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": system_text()}] + messages}
    if tools:
        body["tools"] = to_openai_tools(tools)
    return body


def open_stream(body: dict):
    """Appelle /chat/completions ; 3 relances sur limite de débit, erreur serveur ou réseau."""
    for attempt in range(4):
        req = urllib.request.Request(
            BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}",
                     "User-Agent": f"devagent/{__version__}"})
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (401, 403):
                raise ApiError(f"Clé API invalide ou accès refusé ({e.code}) : {detail}")
            if e.code == 400 and "stream_options" in body and "stream_options" in detail:
                body.pop("stream_options")  # certains fournisseurs ne le gèrent pas
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                try:
                    wait = min(float(e.headers.get("Retry-After", "")), 30)
                except ValueError:
                    wait = 2 ** (attempt + 1)
                print(color(f"  (erreur {e.code}, nouvel essai dans {wait:.0f}s)", "90"))
                time.sleep(wait)
                continue
            if e.code == 429:
                raise ApiError("Limite de requêtes gratuite atteinte : réessayez dans un moment.")
            raise ApiError(f"Erreur API {e.code} : {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise ApiError(f"Connexion impossible à {BASE_URL} : {getattr(e, 'reason', e)}")
    raise ApiError("Échec de l'appel API.")


def read_stream(resp) -> tuple[str, list, dict]:
    """Lit le flux SSE : affiche le texte en direct, retourne (texte, appels d'outils, usage)."""
    text: list[str] = []
    calls: dict[int, dict] = {}
    usage: dict = {}
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("error"):
                raise ApiError(f"Erreur du fournisseur : {chunk['error']}")
            usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    print(delta["content"], end="", flush=True)
                    text.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index")
                    if idx is None:  # certains fournisseurs omettent l'index
                        idx = len(calls) if (tc.get("id") or not calls) else len(calls) - 1
                    slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name") and not slot["name"]:
                        slot["name"] = fn["name"]
                    args = fn.get("arguments") or ""
                    slot["arguments"] += args if isinstance(args, str) else json.dumps(args)
    ordered = [calls[i] for i in sorted(calls)]
    for i, c in enumerate(ordered):
        c["id"] = c["id"] or f"call_{i}"
    return "".join(text), ordered, usage


# --------------------------------------------------------------------------- boucle de l'agent
def agent(messages: list, tools: list) -> None:
    allowed = {t["name"] for t in tools}
    for _ in range(MAX_STEPS):
        text, calls, usage = read_stream(open_stream(build_body(messages, tools)))
        print()

        USAGE["input"] += usage.get("prompt_tokens", 0) or 0
        USAGE["output"] += usage.get("completion_tokens", 0) or 0

        msg: dict = {"role": "assistant", "content": text or (None if calls else "")}
        if calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"],
                                               "arguments": c["arguments"] or "{}"}} for c in calls]
        messages.append(msg)
        if not calls:
            return

        for c in calls:
            try:
                args = json.loads(c["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("un objet JSON est attendu")
            except ValueError as e:
                out = f"Erreur : arguments JSON invalides ({e})."
            else:
                shown = ", ".join(f"{k}={str(v)[:40]!r}" for k, v in args.items())
                print(color(f"  → {c['name']}({shown})", "90"))
                if c["name"] not in allowed:  # garde-fou : --readonly ou outil inventé par le modèle
                    out = f"Erreur : outil « {c['name']} » non disponible."
                else:
                    try:
                        out = FUNCS[c["name"]](**args)
                    except Exception as e:  # l'erreur est renvoyée au modèle, qui s'adapte
                        out = f"Erreur : {e}"
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})

    print(color("⚠  Limite d'étapes atteinte.", "33"))


def chat(tools: list) -> None:
    print("devagent — discussion libre (« exit » ou Ctrl+C pour quitter)")
    messages: list = []
    while True:
        try:
            q = input(color("\nvous > ", "36")).strip()
        except (KeyboardInterrupt, EOFError):
            return
        if q.lower() in {"exit", "quit", "q"}:
            return
        if q:
            messages.append({"role": "user", "content": q})
            agent(messages, tools)


HOOK = """#!/bin/sh
# devagent : relecture automatique avant commit (ne bloque jamais le commit)
python "{script}" review --staged --readonly || true
exit 0
"""


def install_hook() -> None:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    if hook.exists() and not confirm("Un hook pre-commit existe déjà. Le remplacer ?"):
        print("Annulé.")
        return
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK.format(script=Path(__file__).resolve().as_posix()), encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    print(f"Hook installé : {hook}\nIl relit les changements indexés à chaque `git commit` "
          "(au moins 1 appel API par commit). Désactivation : supprimer ce fichier.")


def print_usage() -> None:
    if USAGE["input"] or USAGE["output"]:
        print(color(f"\n[tokens] entrée {USAGE['input']} · sortie {USAGE['output']}", "90"))


def main() -> None:
    global PROVIDER
    ap = argparse.ArgumentParser(prog="devagent", description="Agent IA pour développeurs")
    ap.add_argument("commande", choices=[*PROMPTS, "explain", "hook", "chat"])
    ap.add_argument("cible", nargs="?", help="fichier ou dossier (commande explain)")
    ap.add_argument("--staged", action="store_true", help="review : seulement les changements indexés")
    ap.add_argument("--readonly", action="store_true", help="aucun outil d'écriture ni d'exécution")
    ap.add_argument("--provider", help=f"fournisseur : {', '.join(PROVIDERS)} (défaut {PROVIDER})")
    ap.add_argument("--model", help="nom du modèle (défaut : celui du fournisseur)")
    ap.add_argument("--version", action="version", version=f"devagent {__version__}")
    a = ap.parse_args()

    if a.provider:
        PROVIDER = a.provider.lower()
    if a.commande == "hook":
        return install_hook()
    if a.commande == "explain" and not a.cible:
        ap.error("explain demande une cible : devagent explain <fichier|dossier>")
    configure(a.model)

    tools = [t for t in TOOLS if t["name"] in READONLY] if a.readonly else TOOLS
    print(color(f"Dépôt : {ROOT}  ·  {PROVIDER} / {MODEL}\n", "90"))

    try:
        if a.commande == "chat":
            chat(tools)
        else:
            if a.commande == "explain":
                prompt = (f"Explique `{a.cible}` : rôle, structure, points délicats. "
                          "Utilise read_file et search_code.")
            elif a.commande == "review" and a.staged:
                prompt = ("Relis les changements indexés (get_diff avec staged=true). Signale bugs, "
                          "failles de sécurité et manques de tests. Termine par un verdict.")
            else:
                prompt = PROMPTS[a.commande]
            agent([{"role": "user", "content": prompt}], tools)
    except ApiError as e:
        sys.exit(f"\n{e}")
    except KeyboardInterrupt:
        print("\nInterrompu.")
    finally:
        print_usage()


if __name__ == "__main__":
    main()
