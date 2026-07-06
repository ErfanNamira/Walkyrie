# ✨ Walkyrie: Wallhaven Wallpaper Downloader ⚔️

An interactive, robust, and beautifully rendered command-line tool to search and download wallpapers from Wallhaven.

---

## 🌟 Core Features
- 🔍 **Advanced Search**: Filter by keyword/tag, category, purity, orientation, aspect ratios, exact resolutions, and hex colors.
- 🚫 **Exclude Tags**: Easily filter out unwanted tags.
- 🔄 **Multiple Sort Modes**: Latest, Toplist, Random (with seed support), Views, Favorites, Relevance.
- 🔞 **NSFW Support**: Optional API key integration for NSFW/Sketchy content.
- ⚡ **Concurrent Downloads**: Configurable worker pool with a live, rich progress bar. Respects the 45 req/min API limit.
- ⏸️ **Live Controls**: Pause, resume, or abort downloads on the fly without losing your place.
- 📂 **Custom Folders**: Automatically sorts downloads into custom or query-named folders.

---
## ☄️ How to Install (Easy Way)
1. [Download the latest release.](https://github.com/ErfanNamira/Walkyrie/releases/latest)
2. Run `Walkyrie.exe`.
   
## 💻 Setup (From Source)

Walkyrie requires **Python 3.9+** and relies on two external libraries.

Install the dependencies via pip:
```bash
pip install requests rich
```

## 🛠️ Usage
🧙‍♂️ Interactive Wizard Mode
Simply run the script without arguments to launch the friendly, step-by-step interactive wizard:
```
python Walkyrie.py
```
### 💻 Non-Interactive CLI Mode
Perfect for scripting, cronjobs, or quick downloads. Pass your search parameters directly as arguments:
```
# Download 20 cyberpunk anime wallpapers
python Walkyrie.py --query "cyberpunk" --amount 20 --category anime

# Search and enqueue only (don't download yet)
python Walkyrie.py --query "nature" --no-download

# Resume your saved queue
python Walkyrie.py --resume
```
| Argument | Short | Description |
| :--- | :---: | :--- |
| `--query` | `-q` | Search phrase / tag |
| `--amount` | `-n` | Number of wallpapers to download (omit for all) |
| `--folder` | `-o` | Custom output folder |
| `--category` | | `general`, `anime`, `people`, `all` *(default: `all`)* |
| `--purity` | | `sfw`, `sketchy`, `nsfw`, `sfw+sketchy`, `all` *(default: `sfw`)* |
| `--orientation` | | `any`, `wide`, `portrait` *(default: `any`)* |
| `--sorting` | | `date_added`, `toplist`, `random`, `views`, `favorites`, `relevance` |
| `--top-range` | | Toplist range: `1d`, `3d`, `1w`, `1M`, `3M`, `6M`, `1y` |
| `--seed` | | Seed for random sorting |
| `--exclude` | | Comma-separated tags to exclude |
| `--resolutions` | | Exact resolutions (e.g., `1920x1080,2560x1440`) |
| `--ratios` | | Aspect ratios (e.g., `16x9,16x10`) |
| `--colors` | | Hex color without `#` (e.g., `336600`) |
| `--api-key` | | Wallhaven API key (overrides saved key for this run) |
| `--workers` | `-w` | Concurrent download workers (1-10) |
| `--no-download` | | Only search and enqueue; don't download yet |
| `--resume` | | Skip search; just process whatever is already queued |
| `--clear-queue` | | Clear the pending queue and exit |

## ⌨️ Interactive Controls
### During the Search Wizard
Type b, back, or choose option 0 at any question to abort the wizard and return safely to the Main Menu. Your existing queue and settings are preserved.
### During Downloads
While wallpapers are downloading, you can control the process in real-time by typing the following commands and pressing Enter:

p : ⏸️ Pause (workers finish their current file, then wait)

r : ▶️ Resume a paused download

q : 🛑 Quit/Abort (progress already saved is kept; the rest stays queued for later)

## 📂 Configuration & Storage
Walkyrie keeps your settings and queue organized in your home directory:

Settings (~/.walkyrie/config.json): Stores your API key and default concurrent worker count.

Queue & History (~/.walkyrie/walkyrie.db): SQLite database storing your pending download queue and lifetime download history (to prevent duplicates across different folders).

## 📄 License

MIT
