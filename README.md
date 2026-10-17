# devagent v2

Agent IA en ligne de commande pour développeurs : relecture de code, commits, documentation,
correction de tests, descriptions de pull request. Fonctionne avec des API d'IA gratuites (Groq, Gemini, OpenRouter, Ollama).

## Installation

Aucune dépendance : Python 3.10+ et Git suffisent.

```bash
pip install -e .            # installe la commande `devagent`
# ou, sans installer : python devagent.py <commande>
```

## Choisir une IA gratuite

devagent fonctionne avec toute API compatible « OpenAI chat completions ». Les offres gratuites et
les noms de modèles changent souvent : vérifiez-les sur le site du fournisseur.

| Fournisseur | Clé gratuite | Variable | Lancement |
|---|---|---|---|
| Groq (défaut) | https://console.groq.com | `GROQ_API_KEY` | `devagent review` |
| Google Gemini | https://aistudio.google.com | `GEMINI_API_KEY` | `devagent --provider gemini review` |
| OpenRouter (modèles `:free`) | https://openrouter.ai | `OPENROUTER_API_KEY` | `devagent --provider openrouter review` |
| Ollama (local, 100 % gratuit) | aucune | — | `devagent --provider ollama review` |

```bash
export GROQ_API_KEY="gsk_..."                 # Linux / macOS
$env:GROQ_API_KEY="gsk_..."                   # Windows PowerShell
```

Ollama : installez https://ollama.com, puis `ollama pull llama3.1:8b`. Lancez le serveur avec une
fenêtre de contexte plus large (par défaut elle est trop petite pour lire des diffs) :
`OLLAMA_CONTEXT_LENGTH=16384 ollama serve`.

Autre fournisseur compatible : `DEVAGENT_BASE_URL`, `DEVAGENT_API_KEY` et `DEVAGENT_MODEL`.

## Commandes (depuis n'importe quel dépôt Git)

| Commande | Action |
|---|---|
| `devagent review [--staged]` | Relit les changements (bugs, sécurité, perf, tests) |
| `devagent commit` | Propose un message Conventional Commits et commite après accord |
| `devagent docs` | Met à jour README et docstrings obsolètes |
| `devagent test` | Lance les tests et diagnostique les échecs |
| `devagent fix` | Lance les tests, corrige les échecs avec aperçu du diff |
| `devagent pr` | Rédige titre et description de pull request |
| `devagent explain <cible>` | Explique un fichier ou dossier |
| `devagent hook` | Installe un hook pre-commit de relecture automatique |
| `devagent chat` | Discussion libre sur le dépôt |

Options : `--readonly` (aucun outil d'écriture), `--provider <nom>`, `--model <nom>`, `--version`.

## Nouveautés de la v2

- Réponses en streaming (affichage en direct).
- `edit_file` : modifications ciblées avec **aperçu coloré du diff** avant confirmation.
- Compteur de tokens en fin d'exécution (si le fournisseur les renvoie).
- Commandes `fix`, `pr`, `explain`, `hook` ; option `--readonly` ; `review --staged`.
- Fichier `DEVAGENT.md` à la racine du dépôt : consignes spécifiques au projet (voir l'exemple).
- Gestion propre des erreurs API (clé invalide, limite gratuite atteinte, réseau) et 3 relances automatiques.
- Tests unitaires (`pytest`) sur la sécurité des chemins, l'édition et la confirmation.

## Sécurité

- `edit_file`, `write_file`, `run_tests`, `git_commit` demandent toujours une confirmation.
- Hors terminal interactif (hook, pipe, CI), toute confirmation est automatiquement **refusée**.
- Accès limité au dépôt ; `.git`, `.env*` (sauf `.example`), `.pem`, `.key` refusés.
- Maximum 20 étapes par tâche, sorties tronquées à 20 000 caractères.
- Le contenu envoyé à l'API contient vos diffs et fichiers lus. Certaines offres gratuites utilisent les
  données pour entraîner leurs modèles : évitez le code confidentiel, ou utilisez Ollama (tout reste local).
- Un outil non proposé (ex. en `--readonly`) est refusé même si le modèle essaie de l'appeler.

## Configuration

- `DEVAGENT_PROVIDER` : groq, gemini, openrouter ou ollama (défaut `groq`).
- `DEVAGENT_MODEL` : nom du modèle (sinon celui du fournisseur).
- `DEVAGENT_MAX_OUT` : taille max d'un résultat d'outil en caractères (défaut 20000). Baissez-la
  (ex. 8000) si la limite de tokens par minute d'un plan gratuit est dépassée.
- `DEVAGENT_TEST_CMD` : commande de tests (auto-détection : `php artisan test`, `npm test`, `pytest`).

## Limites des modèles gratuits

Les modèles gratuits sont moins fiables que les gros modèles payants pour appeler les outils et
modifier du code. Relisez toujours l'aperçu du diff avant de confirmer, et préférez `review`,
`explain` et `commit` aux commandes qui modifient des fichiers (`fix`, `docs`).
