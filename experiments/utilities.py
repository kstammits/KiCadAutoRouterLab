
import importlib
import importlib.util
import subprocess
import sys
from typing import Optional, Tuple


# small existing value preserved for compatibility
x = 3.14159


def can_import(module_name: str) -> bool:
	"""Return True if `module_name` can be found (without importing it)."""
	return importlib.util.find_spec(module_name) is not None


def import_module(module_name: str):
	"""Import and return the module object for `module_name`.

	Raises the normal ImportError if it cannot be imported.
	"""
	return importlib.import_module(module_name)


def install_package(package: str, pip_args: Optional[list] = None, upgrade: bool = False,
					user: bool = False, quiet: bool = False) -> Tuple[bool, str]:
	"""Run pip to install `package`. Returns (success, output).

	Uses the current Python executable to run pip: `sys.executable -m pip install ...`.
	"""
	cmd = [sys.executable, "-m", "pip", "install"]
	if upgrade:
		cmd.append("--upgrade")
	if user:
		cmd.append("--user")
	if quiet:
		cmd.append("--quiet")
	if pip_args:
		cmd.extend(pip_args)
	cmd.append(package)
	try:
		out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
		return True, out
	except subprocess.CalledProcessError as e:
		return False, e.output


def ensure_import(module_name: str, install_name: Optional[str] = None, allow_install: bool = True,
				  pip_args: Optional[list] = None, upgrade: bool = False, user: bool = False,
				  quiet: bool = False):
	"""Ensure a module is importable. If not present and `allow_install` is True, run pip.

	- `module_name`: name to import (e.g., "requests").
	- `install_name`: optional pip package name if different (e.g., "mysqlclient" vs "MySQL-python").
	Returns the imported module object on success, or raises an exception on failure.
	"""
	if can_import(module_name):
		return import_module(module_name)

	if not allow_install:
		raise ModuleNotFoundError(f"{module_name} not importable and installation not allowed")

	pkg = install_name or module_name
	ok, out = install_package(pkg, pip_args=pip_args, upgrade=upgrade, user=user, quiet=quiet)
	if not ok:
		raise RuntimeError(f"Failed to install {pkg}:\n{out}")

	if not can_import(module_name):
		raise ModuleNotFoundError(f"Installed {pkg} but still cannot import {module_name}")

	return import_module(module_name)





if __name__ == "__main__":
	# Simple interactive demo: check a module and optionally install it.
	print(f"utilities: x={x}")
	test_mod = input("Module to check (e.g. requests) [leave empty to skip]: ").strip()
	if test_mod:
		if can_import(test_mod):
			print(f"{test_mod} is already importable")
		else:
			ans = input(f"{test_mod} not found — install it now? [y/N]: ").strip().lower()
			if ans == "y":
				success, out = install_package(test_mod)
				if success:
					print(f"Installed {test_mod}")
				else:
					print(f"Installation failed:\n{out}")
			else:
				print("Skipping installation")




