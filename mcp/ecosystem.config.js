module.exports = {
  apps: [
    {
      name: "ghrsst_mcp",
      cwd: "/home/odbadmin/python/ghrsst",
      // Option A (recommended): let PM2 run the script with your Python
      script: "mcp/ghrsst_mcp_server.py",
      interpreter: "/home/odbadmin/.pyenv/versions/py314/bin/python",
      // Option B (alternative): comment the 2 lines above and use the two below instead
      // script: "/home/odbadmin/.pyenv/versions/py314/bin/python",
      // args: "mcp/ghrsst_mcp_server.py",

      // MCP server args
      args: "--transport http --host 0.0.0.0 --port 8765 --path /mcp/ghrsst",

      // Process behavior
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      restart_delay: 2000,
      max_memory_restart: "2G",
      kill_timeout: 5000,

      // Logs
      error_file: "/home/odbadmin/tmp/ghrsst_mcp.err.log",
      out_file: "/home/odbadmin/tmp/ghrsst_mcp.out.log",
      merge_logs: true,
      time: true,

      // Environment
      env: {
        NODE_ENV: "production",
        // GHRSST_API_BASE: "http://127.0.0.1:8035",
        // GHRSST_TIMEOUT_S: "15",
        // GHRSST_POINT_LIMIT: "1000000",
        // GHRSST_DEG_PER_CELL: "0.01"
        // Add AUTH headers if ever needed, e.g.
        // GHRSST_AUTH_HEADER: "Bearer xxx"
      }
    }
  ]
}
