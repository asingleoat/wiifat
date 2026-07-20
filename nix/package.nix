# Plain expression for python3Packages.callPackage, shared by the flake and
# the non-flake NixOS module so each builds wiifat from its own nixpkgs.
{ buildPythonApplication
, setuptools
, evdev
, flask
, matplotlib
, pytestCheckHook
}:

buildPythonApplication {
  pname = "wiifat";
  version = "0.1.0";
  src = ../.;
  pyproject = true;
  build-system = [ setuptools ];
  dependencies = [
    evdev
    flask
    matplotlib
  ];
  nativeCheckInputs = [ pytestCheckHook ];
  enabledTestPaths = [ "tests" ];
}
