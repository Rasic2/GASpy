'''
This submodule contains various functions that operate on both `ase.Atoms`
objects and `pymatgen.Structure` objectsto do various things.
'''

__authors__ = ['Zachary W. Ulissi', 'Kevin Tran']
__emails__ = ['zulissi@andrew.cmu.edu', 'ktran@andrew.cmu.edu']

import warnings
from functools import reduce
import math
import re
import pickle
import numpy as np
import scipy
from scipy.spatial.qhull import QhullError
from ase import Atoms
from ase.build import rotate
from ase.constraints import FixAtoms
from ase.geometry import find_mic
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.ext.matproj import MPRester
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.core.surface import SlabGenerator
from pymatgen.analysis.adsorption import AdsorbateSiteFinder
from pymatgen.analysis.local_env import VoronoiNN
from .utils import unfreeze_dict, read_rc
from .defaults import slab_settings


def make_slabs_from_bulk_atoms(atoms, miller_indices,
                               slab_generator_settings, get_slab_settings):
    '''
    Use pymatgen to enumerate the slabs from a bulk.

    Args:
        atoms                   The `ase.Atoms` object of the bulk that you
                                want to make slabs out of
        miller_indices          A 3-tuple of integers containing the three
                                Miller indices of the slab[s] you want to
                                make.
        slab_generator_settings A dictionary containing the settings to be
                                passed to pymatgen's `SpaceGroupAnalyzer`
                                class.
        get_slab_settings       A dictionary containing the settings to be
                                ppassed to the `get_slab` method of
                                pymatgen's `SpaceGroupAnalyzer` class.
    Returns:
        slabs   A list of the slabs in the form of pymatgen.Structure
                objects. Note that there may be multiple slabs because
                of different shifts/terminations.
    '''
    # Get rid of the `miller_index` argument, which is superceded by the
    # `miller_indices` argument.
    try:
        slab_generator_settings = unfreeze_dict(slab_generator_settings)
        slab_generator_settings.pop('miller_index')
        warnings.warn('You passed a `miller_index` object into the '
                      '`slab_generator_settings` argument for the '
                      '`make_slabs_from_bulk_atoms` function. By design, '
                      'this function will instead use the explicit '
                      'argument, `miller_indices`.', SyntaxWarning)
    except KeyError:
        pass

    struct = AseAtomsAdaptor.get_structure(atoms)
    sga = SpacegroupAnalyzer(struct, symprec=0.1)
    struct_stdrd = sga.get_conventional_standard_structure()
    slab_gen = SlabGenerator(initial_structure=struct_stdrd,
                             miller_index=miller_indices,
                             **slab_generator_settings)
    slabs = slab_gen.get_slabs(**get_slab_settings)
    return slabs


def orient_atoms_upwards(atoms):
    '''
    Orient an `ase.Atoms` object upwards so that the normal direction of the
    surface points in the upwards z direction.

    Arg:
        atoms   An `ase.Atoms` object
    Returns:
        atoms   The same `ase.Atoms` object that was input as an argument,
                except the z-direction should be pointing upwards.
    '''
    # Work on a copy so that we don't modify the original
    atoms = atoms.copy()

    rotate(atoms,
           atoms.cell[2], (0, 0, 1),  # Point the z-direction upwards
           atoms.cell[0], (1, 0, 0),  # Point the x-direction forwards
           rotate_cell=True)
    return atoms


def constrain_slab(atoms, z_cutoff=3.):
    '''
    This function fixes sub-surface atoms of a slab. Also works on systems that
    have slabs + adsorbate(s), as long as the slab atoms are tagged with `0`
    and the adsorbate atoms are tagged with positive integers.

    Inputs:
        atoms       ASE-atoms class of the slab system. The tags of these atoms
                    must be set such that any slab atom is tagged with `0`, and
                    any adsorbate atom is tagged with a positive integer.
        z_cutoff    The threshold to see if slab atoms are in the same plane as
                    the highest atom in the slab
    Returns:
        atoms   A deep copy of the `atoms` argument, but where the appropriate
                atoms are constrained
    '''
    # Work on a copy so that we don't modify the original
    atoms = atoms.copy()

    # We'll be making a `mask` list to feed to the `FixAtoms` class. This list
    # should contain a `True` if we want an atom to be constrained, and `False`
    # otherwise
    mask = []

    # If we assume that the third component of the unit cell lattice is
    # orthogonal to the slab surface, then atoms with higher values in the
    # third coordinate of their scaled positions are higher in the slab. We make
    # this assumption here, which means that we will be working with scaled
    # positions instead of Cartesian ones.
    scaled_positions = atoms.get_scaled_positions()
    unit_cell_height = np.linalg.norm(atoms.cell[2])

    # If the slab is pointing upwards, then fix atoms that are below the
    # threshold
    if atoms.cell[2, 2] > 0:
        max_height = max(position[2] for position, atom in zip(scaled_positions, atoms)
                         if atom.tag == 0)
        threshold = max_height - z_cutoff / unit_cell_height
        for position, atom in zip(scaled_positions, atoms):
            if atom.tag == 0 and position[2] < threshold:
                mask.append(True)
            else:
                mask.append(False)

    # If the slab is pointing downwards, then fix atoms that are above the
    # threshold
    elif atoms.cell[2, 2] < 0:
        min_height = min(position[2] for position, atom in zip(scaled_positions, atoms)
                         if atom.tag == 0)
        threshold = min_height + z_cutoff / unit_cell_height
        for position, atom in zip(scaled_positions, atoms):
            if atom.tag == 0 and position[2] > threshold:
                mask.append(True)
            else:
                mask.append(False)

    else:
        raise RuntimeError('Tried to constrain a slab that points in neither '
                           'the positive nor negative z directions, so we do '
                           'not know which side to fix')

    atoms.constraints += [FixAtoms(mask=mask)]
    return atoms


def is_structure_invertible(structure):
    '''
    This function figures out whether or not an `pymatgen.Structure` object has
    symmetricity.  In this function, the affine matrix is a rotation matrix
    that is multiplied with the XYZ positions of the crystal. If the z,z
    component of that is negative, it means symmetry operation exist, it could
    be a mirror operation, or one that involves multiple rotations/etc.
    Regardless, it means that the top becomes the bottom and vice-versa, and
    the structure is the symmetric.  i.e. structure_XYZ = structure_XYZ*M.

    Arg:
        structure   A `pymatgen.Structure` object.
    Returns
        A boolean indicating whether or not your `ase.Atoms` object is
        symmetric in z-direction (i.e. symmetric with respect to x-y plane).
    '''
    # If any of the operations involve a transformation in the z-direction,
    # then the structure is invertible.
    sga = SpacegroupAnalyzer(structure, symprec=0.1)
    for operation in sga.get_symmetry_operations():
        xform_matrix = operation.affine_matrix
        z_xform = xform_matrix[2, 2]
        if z_xform == -1:
            return True

    return False


def flip_atoms(atoms):
    '''
    Flips an atoms object upside down. Normally used to flip slabs.

    Arg:
        atoms   `ase.Atoms` object
    Returns:
        atoms   The same `ase.Atoms` object that was fed as an argument,
                but flipped upside down.
    '''
    atoms = atoms.copy()

    # This is black magic wizardry to me. Good look figuring it out.
    atoms.wrap()
    atoms.rotate(180, 'x', rotate_cell=True, center='COM')
    if atoms.cell[2][2] < 0.:
        atoms.cell[2] = -atoms.cell[2]
    if np.cross(atoms.cell[0], atoms.cell[1])[2] < 0.0:
        atoms.cell[1] = -atoms.cell[1]
    atoms.wrap()

    return atoms


def tile_atoms(atoms, min_x, min_y):
    '''
    This function will repeat an atoms structure in the x and y direction until
    the x and y dimensions are at least as wide as the given parameters.

    Args:
        atoms   `ase.Atoms` object of the structure that you want to tile
        min_x   The minimum width you want in the x-direction (Angstroms)
        min_y   The minimum width you want in the y-direction (Angstroms)
    Returns:
        atoms_tiled     An `ase.Atoms` object that's just a tiled version of
                        the `atoms` argument.
        (nx, ny)        A 2-tuple containing integers for the number of times
                        the original atoms object was repeated in the x
                        direction and y direction, respectively.
    '''
    x_length = np.linalg.norm(atoms.cell[0])
    y_length = np.linalg.norm(atoms.cell[1])
    nx = int(math.ceil(min_x / x_length))
    ny = int(math.ceil(min_y / y_length))
    n_xyz = (nx, ny, 1)
    atoms_tiled = atoms.repeat(n_xyz)
    return atoms_tiled, (nx, ny)


def find_adsorption_sites(atoms):
    '''
    A wrapper for pymatgen to get all of the adsorption sites of a slab.

    Arg:
        atoms   The slab where you are trying to find adsorption sites in
                `ase.Atoms` format
    Output:
        sites   A `numpy.ndarray` object that contains the x-y-z coordinates of
                the adsorptions sites
    '''
    struct = AseAtomsAdaptor.get_structure(atoms)
    sites_dict = AdsorbateSiteFinder(struct).find_adsorption_sites(put_inside=True)
    sites = sites_dict['all']
    return sites


def find_bulk_cn_dict(bulk_atoms):
    '''
    Get a dictionary of coordination numbers
    for each distinct site in the bulk structure.

    Taken from pymatgen.core.surface Class Slab
    `get_surface_sites`.
    https://pymatgen.org/pymatgen.core.surface.html
    '''
    struct = AseAtomsAdaptor.get_structure(bulk_atoms)
    sga = SpacegroupAnalyzer(struct)
    sym_struct = sga.get_symmetrized_structure()
    unique_indices = [equ[0] for equ in sym_struct.equivalent_indices]
    # Get a dictionary of unique coordination numbers
    # for atoms in each structure.
    # for example, Pt[1,1,1] would have cn=3 and cn=12
    # depends on the Pt atom.
    voronoi_nn = VoronoiNN()
    cn_dict = {}
    for idx in unique_indices:
        elem = sym_struct[idx].species_string
        if elem not in cn_dict.keys():
            cn_dict[elem] = []
        cn = voronoi_nn.get_cn(sym_struct, idx, use_weights=True)
        cn = float('%.5f' % (round(cn, 5)))
        if cn not in cn_dict[elem]:
            cn_dict[elem].append(cn)
    return cn_dict


def find_surface_atoms_indices(bulk_cn_dict, atoms):
    '''
    A helper function referencing codes from pymatgen to
    get a list of surface atoms indices of a slab's
    top surface. Due to how our workflow is setup, the
    pymatgen method cannot be directly applied.

    Taken from pymatgen.core.surface Class Slab,
    `get_surface_sites`.
    https://pymatgen.org/pymatgen.core.surface.html

    Arg:
        bulk_cn_dict    A dictionary of coordination numbers
                        for each distinct site in the respective bulk structure
        atoms           The slab where you are trying to find surface sites in
                        `ase.Atoms` format
    Output:
        indices_list    A list that contains the indices of
                        the surface atoms
    '''
    struct = AseAtomsAdaptor.get_structure(atoms)
    voronoi_nn = VoronoiNN()
    # Identify index of the surface atoms
    indices_list = []
    weights = [site.species.weight for site in struct]
    center_of_mass = np.average(struct.frac_coords,
                                weights=weights, axis=0)

    for idx, site in enumerate(struct):
        if site.frac_coords[2] > center_of_mass[2]:
            try:
                cn = voronoi_nn.get_cn(struct, idx, use_weights=True)
                cn = float('%.5f' % (round(cn, 5)))
                # surface atoms are undercoordinated
                if cn < min(bulk_cn_dict[site.species_string]):
                    indices_list.append(idx)
            except RuntimeError:
                # or if pathological error is returned,
                # indicating a surface site
                indices_list.append(idx)
    return indices_list


def _plane_normal(coords):
    """
    Return the surface normal vector to a plane of best fit
    by performing planar regression.
    See https://gist.github.com/amroamroamro/1db8d69b4b65e8bc66a6
    for the method.

    Arg:
        coords   A `numpy.ndarray` (n,3),
                 coordinates of atoms on the slab surface.

    Output:
        vector  numpy.ndarray. Adsorption vector for an adsorption site.
    """
    A = np.c_[coords[:, 0], coords[:, 1], np.ones(coords.shape[0])]
    vector, _, _, _ = scipy.linalg.lstsq(A, coords[:, 2])
    vector[2] = -1.0
    vector /= -np.linalg.norm(vector)
    return vector


def _ang_between_vectors(v1, v2):
    """
    Returns the angle in degree
    between 3D vectors 'v1' and 'v2'

    Arg:
        v1    3D vector in np.array(x1,y1,z1) form,
              the origin is (0,0,0).
        v1    3D vector in np.array(x2,y2,z2) form,
              the origin is (0,0,0).

    Output:
        angle  angle in degrees.
    """
    cosang = np.dot(v1, v2)
    sinang = np.linalg.norm(np.cross(v1, v2))
    # np.arctan2(sinang, cosang) is angle in radian
    radian = np.arctan2(sinang, cosang)
    angle = radian * 57.2958
    return angle


def find_adsorption_vector(bulk_cn_dict, slab_atoms, surface_indices, adsorption_site):
    """
    Returns the vector of an adsorption site representing the
    furthest distance from the neighboring atoms.
    The vector is a (1,3) numpy array.
    The idea comes from CatKit.
    https://catkit.readthedocs.io/en/latest/?badge=latest

    Arg:
        bulk_cn_dict         A dictionary of coordination numbers
                             for each distinct site in the respective bulk structure
        slab_atoms           The `ase.Atoms` format of a supercell slab.
        surface_indices      The index of the surface atoms in a list.
        adsorption_site      A `numpy.ndarray` object that contains the x-y-z coordinates
                             of the adsorptions sites.

    Output:
        vector            numpy.ndarray. Adsorption vector for an adsorption site.
    """
    vnn = VoronoiNN(allow_pathological=True)

    slab_atoms += Atoms('U', [adsorption_site])
    U_index = slab_atoms.get_chemical_symbols().index('U')
    struct_with_U = AseAtomsAdaptor.get_structure(slab_atoms)
    nn_info = vnn.get_nn_info(struct_with_U, n=U_index)
    nn_indices = [neighbor['site_index'] for neighbor in nn_info]
    surface_nn_indices = [idx for idx in nn_indices if idx in surface_indices]

    # get the index of the closest 4 atom to the site to form a plane
    # chose 4 because it will gaurantee a more accurate plane for edge cases
    nn_dists_from_U = {idx: np.linalg.norm(slab_atoms[idx].position - slab_atoms[U_index].position)
                       for idx in surface_nn_indices}
    sorted_dists = {idx: distance for idx, distance in sorted(nn_dists_from_U.items(), key=lambda item: item[1])}
    closest_4_nn_indices = np.array(list(sorted_dists.keys())[:4], dtype=int)
    plane_coords = struct_with_U.cart_coords[closest_4_nn_indices]
    vector = _plane_normal(plane_coords)

    # Check to see if the vector is reasonable.
    # set an arbitay threshold where the vector and [0, 0, 1]
    # should be less than 60 degrees.
    # If someone has a better way to detect in the future, go for it
    if _ang_between_vectors(np.array([0., 0., 1.]), vector) > 60.:
        message = ('Warning: this might be an edge case where the '
                   'adsorption vector is not appropriate.'
                   ' We will place adsorbates using default [0, 0, 1] vector.')
        warnings.warn(message)
        vector = np.array([0., 0., 1.])

    del slab_atoms[[U_index]]
    return vector


def add_adsorbate_onto_slab(adsorbate, slab, site, constrain: bool = True):
    '''
    There are a lot of small details that need to be considered when adding an
    adsorbate onto a slab. This function will take care of those details for
    you.

    Args:
        adsorbate   An `ase.Atoms` object of the adsorbate
        slab        An `ase.Atoms` object of the slab
        site        A 3-long sequence containing floats that indicate the
                    cartesian coordinates of the site you want to add the
                    adsorbate onto.
    Returns:
        adslab  An `ase.Atoms` object containing the slab and adsorbate.
                The sub-surface slab atoms will be fixed, and all adsorbate
                constraints should be preserved. Slab atoms will be tagged
                with a `0` and adsorbate atoms will be tagged with a `1`.
    '''
    adsorbate = adsorbate.copy()  # To make sure we don't mess with the original
    adsorbate.translate(site)

    adslab = adsorbate + slab
    adslab.cell = slab.cell
    adslab.pbc = [True, True, True]

    # We set the tags of slab atoms to 0, and set the tags of the adsorbate to 1.
    # In future version of GASpy, we intend to set the tags of co-adsorbates
    # to 2, 3, 4... etc (per co-adsorbate)
    tags = [1] * len(adsorbate)
    tags.extend([0] * len(slab))
    adslab.set_tags(tags)

    if constrain:
        # Fix the sub-surface atoms
        adslab_constrained = constrain_slab(adslab)
        return adslab_constrained
    else:
        return adslab


def fingerprint_adslab(atoms):
    '''
    This function will fingerprint a slab+adsorbate atoms object for you.
    Currently, it only works with one adsorbate.

    Arg:
        atoms   `ase.Atoms` object to fingerprint. The slab atoms must be
                tagged with 0 and adsorbate atoms must be tagged with
                non-zero integers.  This function also assumes that the
                first atom in each adsorbate is the binding atom (e.g.,
                of all atoms with tag==1, the first atom is the binding;
                the same goes for tag==2 and tag==3 etc.).
    Returns:
        fingerprint A dictionary whose keys are:
                        coordination            A string indicating the
                                                first shell of
                                                coordinated atoms
                        neighborcoord           A list of strings
                                                indicating the coordination
                                                of each of the atoms in
                                                the first shell of
                                                coordinated atoms
                        nextnearestcoordination A string identifying the
                                                coordination of the
                                                adsorbate when using a
                                                loose tolerance for
                                                identifying "neighbors"
    '''
    # Replace the adsorbate[s] with a single Uranium atom at the first binding
    # site. We need the Uranium there so that pymatgen can find its
    # coordination.
    atoms, binding_positions = remove_adsorbate(atoms)
    atoms += Atoms('U', positions=[binding_positions[1]])
    uranium_index = atoms.get_chemical_symbols().index('U')
    struct = AseAtomsAdaptor.get_structure(atoms)
    try:
        # We have a standard and a loose Voronoi neighbor finder for various
        # purposes
        vnn = VoronoiNN(allow_pathological=True, tol=0.8, cutoff=10)
        vnn_loose = VoronoiNN(allow_pathological=True, tol=0.2, cutoff=10)

        # Find the coordination
        nn_info = vnn.get_nn_info(struct, n=uranium_index)
        coordination = __get_coordination_string(nn_info)

        # Find the neighborcoord
        neighborcoord = []
        for neighbor_info in nn_info:
            # Get the coordination of this neighbor atom, e.g., 'Cu-Cu'
            neighbor_index = neighbor_info['site_index']
            neighbor_nn_info = vnn_loose.get_nn_info(struct, n=neighbor_index)
            neighbor_coord = __get_coordination_string(neighbor_nn_info)
            # Prefix the coordination of this neighbor atom with the identity
            # of the neighber, e.g. 'Cu:Cu-Cu'
            neighbor_element = neighbor_info['site'].species_string
            neighbor_coord_labeled = neighbor_element + ':' + neighbor_coord
            neighborcoord.append(neighbor_coord_labeled)

        # Find the nextnearestcoordination
        nn_info_loose = vnn_loose.get_nn_info(struct, n=uranium_index)
        nextnearestcoordination = __get_coordination_string(nn_info_loose)

        return {'coordination': coordination,
                'neighborcoord': neighborcoord,
                'nextnearestcoordination': nextnearestcoordination}
    # If we get some QHull or ValueError, then just assume that the adsorbate desorbed
    except (QhullError, ValueError):
        return {'coordination': '',
                'neighborcoord': '',
                'nextnearestcoordination': ''}


def remove_adsorbate(adslab):
    '''
    This function removes adsorbates from an adslab and gives you the locations
    of the binding atoms. Note that we assume that the first atom in each adsorbate
    is the binding atom.

    Arg:
        adslab  The `ase.Atoms` object of the adslab. The adsorbate atom(s) must
                be tagged with non-zero integers, while the slab atoms must be
                tagged with zeroes. We assume that for each adsorbate, the first
                atom (i.e., the atom with the lowest index) is the binding atom.
    Returns:
        slab                The `ase.Atoms` object of the bare slab.
        binding_positions   A dictionary whose keys are the tags of the
                            adsorbates and whose values are the cartesian
                            coordinates of the binding site.
    '''
    # Operate on a local copy so we don't propagate changes to the original
    slab = adslab.copy()

    # Remove all the constraints and then re-constrain the slab. We do this
    # because ase does not like it when we delete atoms with constraints.
    slab.set_constraint()
    slab = constrain_slab(slab)

    # Delete atoms in reverse order to preserve correct indexing
    binding_positions = {}
    for i, atom in reversed(list(enumerate(slab))):
        if atom.tag != 0:
            binding_positions[atom.tag] = atom.position
            del slab[i]

    return slab, binding_positions


def __get_coordination_string(nn_info):
    '''
    This helper function takes the output of the `VoronoiNN.get_nn_info` method
    and gives you a standardized coordination string.

    Arg:
        nn_info     The output of the
                    `pymatgen.analysis.local_env.VoronoiNN.get_nn_info` method.
    Returns:
        coordination    A string indicating the coordination of the site
                        you fed implicitly through the argument, e.g., 'Cu-Cu-Cu'
    '''
    coordinated_atoms = [neighbor_info['site'].species_string
                         for neighbor_info in nn_info
                         if neighbor_info['site'].species_string != 'U']
    coordination = '-'.join(sorted(coordinated_atoms))
    return coordination


def calculate_unit_slab_height(atoms, miller_indices, slab_generator_settings=None):
    '''
    Calculates the height of the smallest unit slab from a given bulk and
    Miller cut

    Args:
        atoms                   An `ase.Atoms` object of the bulk you want to
                                make a surface out of
        miller_indices          A 3-tuple of integers representing the Miller
                                indices of the surface you want to make
        slab_generator_settings A dictionary that can be passed as kwargs to
                                instantiate the
                                `pymatgen.core.surface.SlabGenerator` class.
                                Defaults to the settings in
                                `gaspy.defaults.slab_settings`.
    Returns:
        height  A float corresponding the height (in Angstroms) of the smallest
                unit slab
    '''
    if slab_generator_settings is None:
        slab_generator_settings = slab_settings()['slab_generator_settings']
        # We don't care about these things
        del slab_generator_settings['min_vacuum_size']
        del slab_generator_settings['min_slab_size']

    # Instantiate a pymatgen `SlabGenerator`
    structure = AseAtomsAdaptor.get_structure(atoms)
    sga = SpacegroupAnalyzer(structure, symprec=0.1)
    structure = sga.get_conventional_standard_structure()
    gen = SlabGenerator(initial_structure=structure,
                        miller_index=miller_indices,
                        min_vacuum_size=0.,
                        min_slab_size=0.,
                        **slab_generator_settings)

    # Get and return the height
    height = gen._proj_height
    return height


def find_max_movement(atoms_initial, atoms_final):
    '''
    Given ase.Atoms objects, find the furthest distance that any single atom in
    a set of atoms traveled (in Angstroms)

    Args:
        initial_atoms   `ase.Atoms` of the structure in its initial state
        final_atoms     `ase.Atoms` of the structure in its final state
    Returns:
        max_movement    A float indicating the further movement of any single atom
                        before and after relaxation (in Angstroms)
    '''
    # Calculate the distances for each atom
    distances = atoms_final.positions - atoms_initial.positions

    # Reduce the distances in case atoms wrapped around (the minimum image
    # convention)
    _, movements = find_mic(distances, atoms_final.cell, atoms_final.pbc)
    max_movement = max(movements)

    return max_movement


def get_stoich_from_mpid(mpid):
    '''
    Get the reduced stoichiometry of a Materials Project bulk material.

    Arg:
        mpid    A string for the Materials Project ID numbers---e.g.,
                'mp-12802'
    Returns:
        stoich  A dictionary whose keys are the elements and whose
                values are ints of the stoichiometry of that given
                element---e.g., {'Al': 1, 'Cu': 3}
    '''
    # Load the cache of it exists
    cache_name = read_rc('gasdb_path') + '/mp_stoichs/' + mpid + '.pkl'
    try:
        with open(cache_name, 'rb') as file_handle:
            stoich = pickle.load(file_handle)

    except (FileNotFoundError, EOFError):
        # Get the formula from Materials Project. It'll come out like "CuAl2"
        # or something.
        with MPRester(read_rc('matproj_api_key')) as rester:
            docs = rester.query({'task_ids': mpid}, ['full_formula'])
        formula = docs[0]['full_formula']

        # Split the formula up by each element, e.g., ['Cu', 'Al2']
        element_counts = re.findall('[A-Z][^A-Z]*', formula)

        # Parse each of the elements out into the format we want
        stoich = {}
        for element_count in element_counts:
            element_string = element_count.rstrip('0123456789')
            count = element_count[len(element_string):]
            stoich[element_string] = int(count)

        # Divide the counts by the greatest common denominator to simplify the
        # formula
        gcd = reduce(math.gcd, stoich.values())
        for element, count in stoich.items():
            stoich[element] = count / gcd

        # Cache it because this stuff because querying MP takes awhile
        with open(cache_name, 'wb') as file_handle:
            pickle.dump(stoich, file_handle)
    return stoich
