import os
import copy
import argparse
import xml.etree.ElementTree as ET
import sys


def load_packages(lib_path):
    tree = ET.parse(lib_path)
    root = tree.getroot()
    packages = root.find(".//packages")
    pkg_dict = {}

    for pkg in packages.findall("package"):
        name = pkg.attrib.get("name")
        if name:
            pkg_dict[name] = pkg

    return tree, root, packages, pkg_dict


def replace_packages_in_library(lib_path, ref_packages):
    tree = ET.parse(lib_path)
    root = tree.getroot()
    packages = root.find(".//packages")

    if packages is None:
        return

    existing = {pkg.attrib.get("name"): pkg for pkg in packages.findall("package")}

    replaced = False

    for name, ref_pkg in ref_packages.items():
        if name in existing:
            print(f"[INFO] Replacing {name} in {lib_path}")
            packages.remove(existing[name])
            packages.append(copy.deepcopy(ref_pkg))
            replaced = True

    if replaced:
        tree.write(lib_path, encoding="utf-8", xml_declaration=True)


def process_libraries(reflibname, libdir):
    _, _, _, ref_packages = load_packages(reflibname)

    for file in os.listdir(libdir):
        if file.endswith(".lbr"):
            full_path = os.path.join(libdir, file)

            if os.path.abspath(full_path) == os.path.abspath(reflibname):
                continue

            replace_packages_in_library(full_path, ref_packages)


def main():
    parser = argparse.ArgumentParser(
        description="Replace matching footprints in EAGLE libraries"
    )
    parser.add_argument("reflib", help="Path to reference .lbr file")
    parser.add_argument("libdir", help="Directory with user libraries")

    # Если аргументы не переданы вообще
    if len(sys.argv) == 1:
        parser.print_help()
        print("\nExample:")
        print("  python script.py reference.lbr ./libs")
        sys.exit(1)

    args = parser.parse_args()

    # Проверки
    if not os.path.isfile(args.reflib):
        print(f"[ERROR] Reference library not found: {args.reflib}")
        print("\nExample:")
        print("  python script.py reference.lbr ./libs")
        sys.exit(1)

    if not args.reflib.endswith(".lbr"):
        print(f"[ERROR] Reference file must be .lbr: {args.reflib}")
        sys.exit(1)

    if not os.path.isdir(args.libdir):
        print(f"[ERROR] Library directory not found: {args.libdir}")
        print("\nExample:")
        print("  python script.py reference.lbr ./libs")
        sys.exit(1)

    process_libraries(args.reflib, args.libdir)


if __name__ == "__main__":
    main()