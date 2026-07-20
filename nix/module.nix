# NixOS module for the wiifat scale server. Importable directly from a
# non-flake configuration:
#
#   imports = [ /path/to/wiifat/nix/module.nix ];
#   services.wiifat = { enable = true; host = "0.0.0.0"; openFirewall = true; };
#
# The flake's nixosModules.default is this same file.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.wiifat;
in
{
  options.services.wiifat = {
    enable = lib.mkEnableOption "WiiFat scale and local web dashboard";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python3Packages.callPackage ./package.nix { };
      defaultText = lib.literalExpression
        "pkgs.python3Packages.callPackage ./package.nix { }";
      description = "wiifat package to run.";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 8480;
      description = "HTTP listen port.";
    };
    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "HTTP listen address; use 0.0.0.0 for LAN access.";
    };
    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the configured HTTP port in the firewall.";
    };
    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Service user, or null for a systemd DynamicUser.";
    };
    dbPath = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/wiifat/wiifat.sqlite3";
      description = "SQLite database path.";
    };
    calibrationPath = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/wiifat/calibration.json";
      description = "Calibration JSON path.";
    };
    idleTimeout = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 15;
      description = "Seconds idle after a weigh-in before disconnecting.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.wiifat = {
      description = "WiiFat Balance Board scale and web dashboard";
      after = [ "bluetooth.service" ];
      wants = [ "bluetooth.service" ];
      wantedBy = [ "multi-user.target" ];
      path = [ pkgs.bluez ];
      environment.MPLCONFIGDIR = "/tmp/wiifat-matplotlib";
      serviceConfig = {
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/wiifat"
          "serve"
          "--host" cfg.host
          "--port" (toString cfg.port)
          "--db" cfg.dbPath
          "--config" cfg.calibrationPath
          "--idle-timeout" (toString cfg.idleTimeout)
        ];
        SupplementaryGroups = [ "input" ];
        Restart = "on-failure";
        RestartSec = 5;
      } // lib.optionalAttrs (cfg.port < 1024) {
        # Non-root bind to a privileged port (e.g. 80).
        AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
      } // lib.optionalAttrs (cfg.user == null) {
        DynamicUser = true;
        StateDirectory = "wiifat";
      } // lib.optionalAttrs (cfg.user != null) {
        DynamicUser = false;
        User = cfg.user;
      };
    };

    networking.firewall.allowedTCPPorts =
      lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
