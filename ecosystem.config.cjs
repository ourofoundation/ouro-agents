const path = require("path");
const os = require("os");

/** Directory containing this file (ouro-agents package root). */
const appDir = __dirname;

/** Pyenv install root; override with PYENV_ROOT when generating the file or in the shell. */
const pyenvRoot = process.env.PYENV_ROOT || path.join(os.homedir(), ".pyenv");

/**
 * Virtualenv / version name from .python-version (pyenv local).
 * Change this string if your pyenv version name differs.
 */
const pyenvVersion = "agents";

const pythonBin = path.join(pyenvRoot, "versions", pyenvVersion, "bin", "python");

function agentApp(name, configFile, envFile) {
  return {
    name: `${name}-agent`,
    cwd: appDir,
    script: pythonBin,
    interpreter: "none",
    args: ["-m", "ouro_agents.runner", "--config", configFile, "--env-file", path.join(appDir, envFile), "serve"],
    exec_mode: "fork",
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: "2G",
    env: {
      PYTHONPATH: appDir,
      PYENV_ROOT: pyenvRoot,
      ENV_FILE: path.join(appDir, envFile),
    },
    env_production: {
      NODE_ENV: "production",
      PYTHON_ENV: "production",
    },
    time: true,
    log_date_format: "YYYY-MM-DD HH:mm:ss Z",
  };
}

module.exports = {
  apps: [
    agentApp("hermes", "hermes.json", ".env.hermes"),
    agentApp("athena", "athena.json", ".env.athena"),
  ],
};
