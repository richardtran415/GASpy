"""
This submodule is part of GASpy. It is meant to be used by FireWorks to perform
VASP calculations.
"""

__authors__ = ["Zachary W. Ulissi", "Kevin Tran"]
__emails__ = ["zulissi@andrew.cmu.edu", "ktran@andrew.cmu.edu"]

import os
import uuid
import binascii
import numpy as np
import ase.io
from ase.io.trajectory import TrajectoryWriter
from ase.optimize import BFGS
from ase.calculators.vasp import Vasp2
from ase.calculators.singlepoint import SinglePointCalculator as SPC

# TODO:  Need to handle setting the pseudopotential directory, probably in the
# submission config if it stays constant? (vasp_qadapter.yaml)


def runVasp(fname_in, fname_out, vasp_flags):
    """
    This function is meant to be sent to each cluster and then used to run our
    rockets. As such, it has algorithms to run differently depending on the
    cluster that is trying to use this function.

    Args:
        fname_in    A string indicating the file name of the initial structure.
                    This file should be readable by `ase.io.read`.
        fname_out   A string indicating the name of the file you want to save
                    the final, relaxed structure to.
        vasp_flags  A dictionary of settings we want to pass to the `Vasp2`
                    calculator
    Returns:
        atoms_str   A string-formatted name for the atoms
        traj_hex    A string-formatted hex enocding of the entire relaxation
                    trajectory
        energy      A float indicating the potential energy of the final image
                    in the relaxation [eV]
    """
    # Read the input atoms object
    atoms = ase.io.read(str(fname_in))

    # Perform the relaxation
    final_image = _perform_relaxation(atoms, vasp_flags, fname_out)

    # Parse and return output
    atoms_str = str(atoms)
    with open('all.traj', 'rb') as fhandle:
        traj_hex = binascii.hexlify(fhandle.read()).decode("utf-8")
    energy = final_image.get_potential_energy()
    return atoms_str, traj_hex, energy


def _perform_relaxation(atoms, vasp_flags, fname_out):
    """
    This function will perform the DFT relaxation while also saving the final
    image for you.

    Args:
        atoms       `ase.Atoms` object of the structure we want to relax
        vasp_flags  A dictionary of settings we want to pass to the `Vasp2`
                    calculator
        fname_out   A string indicating the file name you want to use when
                    saving the final, relaxed structure
    Returns:
        atoms   The relaxed `ase.Atoms` structure
    """
    # Initialize some things before we run
    atoms, vasp_flags = _clean_up_vasp_inputs(atoms, vasp_flags)
    if 'NCORE' in os.environ:
        vasp_flags['ncore'] = int(float(os.environ['NCORE']))
    if 'KPAR' in os.environ:
        vasp_flags['kpar'] = int(float(os.environ['KPAR']))

    # Detect whether or not there are constraints that cannot be handled by VASP
    allowable_constraints = {"FixAtoms"}
    vasp_compatible = True
    for constraint in atoms.constraints:
        if constraint.todict()["name"] not in allowable_constraints:
            vasp_compatible = False
            break

    # Run with VASP by default
    if vasp_compatible:
        final_image = _relax_with_vasp(atoms, vasp_flags)
    # If VASP can't handle it, then use ASE/VASP together
    else:
        final_image = _relax_with_ase(atoms, vasp_flags)

    # Save the last image
    final_image.write(str(fname_out))
    return final_image


def _clean_up_vasp_inputs(atoms, vasp_flags):
    """
    There are some VASP settings that are used across all our clusters. This
    function takes care of these settings.

    Arg:
        atoms       `ase.Atoms` object of the structure we want to relax
        vasp_flags  A dictionary of settings we want to pass to the `Vasp2`
                    calculator
    Returns:
        atoms       `ase.Atoms` object of the structure we want to relax, but
                    with the unit vectors fixed (if needed)
        vasp_flags  A modified version of the 'vasp_flags' argument
    """
    # Check that the unit vectors obey the right-hand rule, (X x Y points in
    # Z). If not, then flip the order of X and Y to enforce this so that VASP
    # is happy.
    if np.dot(np.cross(atoms.cell[0], atoms.cell[1]), atoms.cell[2]) < 0:
        atoms.set_cell(atoms.cell[[1, 0, 2], :])

    # Set the pseudopotential type by setting 'xc' in Vasp()
    if vasp_flags["pp"].lower() == "lda":
        vasp_flags["xc"] = "lda"
    elif vasp_flags["pp"].lower() == "pbe":
        vasp_flags["xc"] = "PBE"

    # Push the pseudopotentials into the OS environment for VASP to pull from
    pseudopotential = vasp_flags["pp_version"]
    os.environ["VASP_PP_PATH"] = (
        os.environ["VASP_PP_BASE"] + "/" + str(pseudopotential) + "/"
    )
    del vasp_flags["pp_version"]

    return atoms, vasp_flags


def _relax_with_ase(atoms, vasp_flags):
    """
    Instead of letting VASP handle the relaxation autonomously, we instead use
    VASP only as an eletronic structure calculator and use ASE's BFGS to
    perform the atomic position optimization.

    Note that this will also write the trajectory to the 'all.traj' file and
    save the log file as 'relax.log'.

    Args:
        atoms       `ase.Atoms` object of the structure we want to relax
        vasp_flags  A dictionary of settings we want to pass to the `Vasp2`
                    calculator
    Returns:
        atoms   The relaxed `ase.Atoms` structure
    """
    vasp_flags["ibrion"] = 2
    vasp_flags["nsw"] = 0
    calc = Vasp2(**vasp_flags)
    atoms.set_calculator(calc)
    optimizer = BFGS(atoms, logfile="relax.log", trajectory="all.traj")
    optimizer.run(fmax=vasp_flags["ediffg"] if "ediffg" in vasp_flags else 0.05)
    return atoms


def _relax_with_vasp(atoms, vasp_flags):
    """
    Perform a DFT relaxation with VASP and then write the trajectory to the
    'all.traj' file and save the log file.

    Args:
        atoms       `ase.Atoms` object of the structure we want to relax
        vasp_flags  A dictionary of settings we want to pass to the `Vasp2`
                    calculator
    Returns:
        atoms   The relaxed `ase.Atoms` structure
    """
    # Run the calculation
    calc = Vasp2(**vasp_flags)
    atoms.set_calculator(calc)
    atoms.get_potential_energy()

    # Read the trajectory from the output file
    images = []
    for atoms in ase.io.read("vasprun.xml", ":"):
        image = atoms.copy()
        image = image[calc.resort]
        image.set_calculator(
            SPC(
                image,
                energy=atoms.get_potential_energy(),
                forces=atoms.get_forces()[calc.resort],
            )
        )
        images += [image]

    # Write the trajectory
    with TrajectoryWriter("all.traj", "a") as tj:
        for atoms in images:
            tj.write(atoms)
    return images[-1]


def atoms_to_hex(atoms):
    """
    Turn an atoms object into a hex string so that we can pass it through fireworks

    Arg:
        atoms   The `ase.Atoms` object that you want to hex encode
    Returns:
        _hex    A hex string of the `ase.Atoms` object
    """
    # We need to write the atoms object into a file before encoding it. But we don't
    # want multiple calls to this function to interfere with each other, so we generate
    # a random file name via uuid to reduce this risk. Then we delete it.
    fname = str(uuid.uuid4()) + ".traj"
    atoms.write(fname)
    with open(fname, "rb") as fhandle:
        try:
            _hex = binascii.hexlify(fhandle.read()).decode("utf-8")
            os.remove(fname)
        except OSError:
            pass

    return _hex


#decode_hex = codecs.getdecoder("hex_codec")


def hex_to_file(file_name, hex_):
    """
    Write a hex string into a file. One application is to unpack hexed atoms
    pobjects in local fireworks job directories

    Args:
        file_name   A string indicating the name of the file you want to write
                    to
        hex_        A hex string of the object you want to write to the file
    """
    with open(file_name, 'wb') as fhandle:
        unhex_str = binascii.unhexlify(hex_)
        # b = str(unhex_str)[2:-1]
        # b1 = bytes(b, 'utf-8')
        fhandle.write(unhex_str)
