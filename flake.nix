{
  description = "Wii Balance Board on Linux: pairing, live monitoring, and a logging scale";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
      mkPackage = pkgs: pkgs.python3Packages.callPackage ./nix/package.nix { };
    in
    {
      packages = forAllSystems (pkgs: {
        default = mkPackage pkgs;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.system}.default}/bin/wiifat";
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              evdev
              flask
              matplotlib
              pytest
            ]))
            pkgs.xwiimote # xwiishow / xwiidump for low-level debugging
          ];
        };
      });

      nixosModules.default = import ./nix/module.nix;
    };
}
