# Contributing to AI Second Brain Toolkit

Thank you for your interest in contributing! We welcome issues, documentation improvements, bug fixes, and feature proposals that align with our core design principles.

---

## 🧭 Core Principles (Non-Negotiable)

Every contribution must honor these three foundational tenets:
1. **$0 Cost & No Paid Subscriptions:** The toolkit must always remain fully operational using free, local tools (Python stdlib, Obsidian, and local desktop AI interfaces). PRs introducing mandatory paid SaaS backends or cloud vector stores will not be accepted.
2. **Fail-Closed Privacy & Zero PII:** The toolkit runs locally over stdio IPC. It must never expose webhooks, open listening ports to the public internet, or send personal host paths (`C:\Users\<username>`) or raw vault data to external services.
3. **Synthetic Test Fixtures Only:** **NEVER commit personal notes, private conversation transcripts, or real API keys.** All automated unit tests must generate and clean up temporary synthetic directories.

---

## 🛠️ Development Setup

### 1. Prerequisites
- Python 3.11, 3.12, or 3.13 (CPython)
- Windows PowerShell, macOS zsh, or Linux bash
- Git

### 2. Getting Started
1. **Fork** the repository on GitHub.
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/<your-username>/ai-second-brain-toolkit.git
   cd ai-second-brain-toolkit
   ```
3. **Verify Environment Health:**
   ```powershell
   # Windows:
   .\scripts\install.ps1 --check-only

   # Linux / macOS:
   python scripts/install_toolkit.py --check-only
   ```
4. **Run the Test Suite:**
   ```bash
   python -m unittest discover -s tests -v
   ```

---

## 📋 Pull Request Guidelines

1. **Create a Feature Branch:**
   ```bash
   git checkout -b feature/my-improvement
   ```
2. **Follow Code Conventions:**
   - Use standard Python type annotations (`typing`, `pathlib.Path`).
   - Keep functions focused and bounded.
   - Include docstrings explaining security or boundary contracts.
   - Maintain Win32 and POSIX filesystem safety (handle path separators, case-insensitivity, and reserved device names).
3. **Add Tests:**
   - Every bug fix or new feature must include a corresponding unit test in `tests/`.
   - Tests must run offline without internet connectivity.
4. **Verify Quality Before Submitting:**
   - Ensure all 458+ unit tests pass.
   - Run `.\scripts\install.ps1 --check-only` to ensure no diagnostic regressions.
5. **Write Clear Commit Messages:**
   - Use imperative, conventional commit messages:  
     `feat: Add ...`, `fix: Resolve ...`, `docs: Update ...`, `security: Harden ...`.

---

## 🔒 Security Vulnerabilities

If you identify a security issue, please consult [SECURITY.md](SECURITY.md) to report it privately rather than creating a public issue.

---

## 📄 License

By contributing to this project, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).
