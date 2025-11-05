module.exports = {
  apps : [
  {
    name: 'ghrsst',
    script: './conf/start_app.sh',
    args: '',
    merge_logs: true,
    autorestart: true,
    log_file: "/home/odbadmin/tmp/ghrsst.outerr.log",
    out_file: "/home/odbadmin/tmp/ghrsst_app.log",
    error_file: "/home/odbadmin/tmp/ghrsst_err.log",
    log_date_format : "YYYY-MM-DD HH:mm Z",
    append_env_to_name: true,
    watch: false,
    max_memory_restart: '4G',
    pre_stop: "ps -ef | grep -w 'ghrsst_app' | grep -v grep | awk '{print $2}' | xargs -r kill -9"
  }],
};
