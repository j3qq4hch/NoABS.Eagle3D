# NoABS.Eagle3D (No Autodesk BS Eagle3D)

Russian version can be found [here](readme_ru.md)

NoABS.Eagle3D is a local and fast 3D visualization tool for PCBs in EagleCAD.
NoABS.Eagle3D consists of several scripts that implement board export to 3D (both as a textured preview and in STEP format for import into mCAD) and the **glue** utility, designed to associate 3D models with footprints from Eagle libraries.

## Linking Models to Libraries

This is handled by the **glue** utility. The process of "gluing" a 3D model to a library involves writing a small amount of metadata into the `description` tag of footprints in the library file, used to correctly orient the component model on the footprint. This has no effect on the library's functionality in Eagle. All component model files must be stored in the `step` folder inside the glue utility's directory. The names of the STEP models must match the names of the footprints in the library. A footprint with a "glued" model can be copied from one library to another, and the link between the footprint and the model is preserved.

## Reference Footprint Library

Although NoABS.Eagle3D does not include any libraries itself, you can use the [NoABS.Libs](https://github.com/j3qq4hch/NoABS.Libs) repository, which contains a small set of Eagle component libraries, some of whose footprints already have 3D models attached. This repository includes a so-called reference footprint library `reference_packages_3d.lbr`, which contains aesthetically pleasing and technically correct footprints for various components with manually curated 3D models attached.

## Usage Scenario

The idea is that you copy footprints from the reference library into your own libraries, overwriting your footprints with the reference ones (which are known to be good) along with their attached 3D models.

That said, nothing prevents users from modifying the reference footprints themselves or from attaching models to their own libraries directly using the glue utility. The reference library imposes nothing — it only offers a small convenience.

## Automatic Propagation of Changes

Since there is usually more than one library using the same footprints, it is convenient to be able to copy a reference footprint to all such libraries at once. For this purpose there is the `downstream.py` script, which takes the list of all footprints from the reference library and replaces all matching footprints in all libraries in a specified directory.
This functionality is also built into the glue utility.

For more details on usage, see the [usage guide](USAGE_EN.md).
