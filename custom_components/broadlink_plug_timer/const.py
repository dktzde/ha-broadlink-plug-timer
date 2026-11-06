"""Constants of the Broadlink Plug Timer integration."""

DOMAIN = "broadlink_plug_timer"
TITLE = "Broadlink Plug Timer"

#: The plugs themselves are set up by the core Broadlink integration; host, MAC and device type
#: are taken from its config entry (keys below), so nothing has to be entered twice.
BROADLINK_DOMAIN = "broadlink"
CONF_BROADLINK_HOST = "host"
CONF_BROADLINK_MAC = "mac"
CONF_BROADLINK_TYPE = "type"

#: Folder below the Home Assistant configuration folder for memory backups (raw 512 bytes).
BACKUP_FOLDER = DOMAIN

# Actions
SERVICE_READ = "read"
SERVICE_SET_DEADMAN = "set_deadman"
SERVICE_CLEAR_DEADMAN = "clear_deadman"
SERVICE_SET_ONCE = "set_once"
SERVICE_WRITE_WEEKLY = "write_weekly"
SERVICE_BACKUP = "backup"
SERVICE_RESTORE = "restore"

# Action fields
ATTR_MINUTES = "minutes"
ATTR_ON_TIME = "on_time"
ATTR_OFF_TIME = "off_time"
ATTR_ENTRIES = "entries"
ATTR_DRY_RUN = "dry_run"
ATTR_FILE = "file"
ATTR_ENABLED = "enabled"
ATTR_DAYS = "days"
ATTR_NOTE = "note"
