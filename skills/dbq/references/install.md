# dbq installation

1. Run `dbq --version`. This Skill version requires CLI `>=0.3,<0.4`.
2. If dbq is missing, prefer an isolated uv tool installation. Never modify the system Python or request administrator access.
3. Install a fixed release tag or commit from the public source repository. Do not silently install mutable `main`:

   ```text
   uv tool install "git+https://github.com/yinpengfei/skills.git@TAG"
   ```

4. When working in a checked-out source repository, use `uv tool install .`.
5. Run `dbq --version` and `dbq doctor --json` after installation. Do not initialize a vault or add a connection merely to test installation.
6. If uv is unavailable, use pipx or a Python 3.11+ virtual environment. Keep database drivers inside that isolated environment.

Installation does not create credentials. `dbq init` is a separate, user-visible configuration step.
