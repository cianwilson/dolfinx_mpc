# Copyright (C) 2020-2021 Jørgen S. Dokken
#
# This file is part of DOLFINX_MPC
#
# SPDX-License-Identifier:    MIT

from typing import List, Tuple

import dolfinx.fem as _fem
import dolfinx.jit as _jit
import dolfinx.cpp as _cpp
import numpy
import ufl
from dolfinx.common import Timer
from dolfinx_mpc.multipointconstraint import (MultiPointConstraint,
                                              cpp_dirichletbc)
from petsc4py import PETSc as _PETSc

import numba

from .helpers import extract_slave_cells, pack_slave_facet_info
from .numba_setup import initialize_petsc, sink

mode = _PETSc.InsertMode.ADD_VALUES
insert = _PETSc.InsertMode.INSERT_VALUES
ffi, set_values_local = initialize_petsc()


def assemble_matrix(form: ufl.form.Form, constraint: MultiPointConstraint,
                    bcs: List[_fem.DirichletBC] = [], diagval=1, A: _PETSc.Mat = None,
                    form_compiler_parameters={}, jit_parameters={}):
    """
    Assembles a ufl form given a multi point constraint and possible
    Dirichlet boundary conditions.
    NOTE: Strong Dirichlet conditions cannot be on master dofs.

    Parameters
    ----------
    form
        The bilinear variational form
    constraint
        The multi point constraint
    bcs
        List of Dirichlet boundary conditions
    diagval
        Value to set on the diagonal of the matrix (Default 1)
    A
        PETSc matrix to assemble into (optional)
    form_compiler_parameters
        Parameters used in FFCx compilation of this form. Run `ffcx --help` at
        the commandline to see all available options. Takes priority over all
        other parameter values, except for `scalar_type` which is determined by
        DOLFINx.
    jit_parameters
        Parameters used in CFFI JIT compilation of C code generated by FFCx.
        See `python/dolfinx/jit.py` for all available parameters.
        Takes priority over all other parameter values.

    """
    timer_matrix = Timer("~MPC: Assemble matrix (numba)")

    V = constraint.function_space
    dofmap = V.dofmap
    dofs = dofmap.list.array

    # Pack MPC data for numba kernels
    coefficients = constraint.coefficients()[0]
    masters_adj = constraint.masters
    c_to_s_adj = constraint.cell_to_slaves
    cell_to_slave = c_to_s_adj.array
    c_to_s_off = c_to_s_adj.offsets
    is_slave = constraint.is_slave
    mpc_data = (masters_adj.array, coefficients, masters_adj.offsets, cell_to_slave, c_to_s_off, is_slave)
    slave_cells = extract_slave_cells(c_to_s_off)

    # Create 1D bc indicator for matrix assembly
    num_dofs_local = (dofmap.index_map.size_local + dofmap.index_map.num_ghosts) * dofmap.index_map_bs
    is_bc = numpy.zeros(num_dofs_local, dtype=bool)
    if len(bcs) > 0:
        for bc in cpp_dirichletbc(bcs):
            is_bc[bc.dof_indices()[0]] = True

    # Get data from mesh
    pos = V.mesh.geometry.dofmap.offsets
    x_dofs = V.mesh.geometry.dofmap.array
    x = V.mesh.geometry.x

    # If using DOLFINx complex build, scalar type in form_compiler parameters must be updated
    is_complex = numpy.issubdtype(_PETSc.ScalarType, numpy.complexfloating)
    if is_complex:
        form_compiler_parameters["scalar_type"] = "double _Complex"

    # Generate ufc_form
    ufc_form, _, _ = _jit.ffcx_jit(V.mesh.comm, form,
                                   form_compiler_parameters=form_compiler_parameters,
                                   jit_parameters=jit_parameters)

    # Generate matrix with MPC sparsity pattern
    cpp_form = _fem.Form(form, form_compiler_parameters=form_compiler_parameters,
                         jit_parameters=jit_parameters)._cpp_object

    # Pack constants and coefficients
    form_coeffs = _cpp.fem.pack_coefficients(cpp_form)
    form_consts = _cpp.fem.pack_constants(cpp_form)

    # Create sparsity pattern and matrix if not supplied
    if A is None:

        pattern = constraint.create_sparsity_pattern(cpp_form)
        pattern.assemble()
        A = _cpp.la.create_matrix(V.mesh.comm, pattern)
    A.zeroEntries()

    # Assemble the matrix with all entries
    _cpp.fem.assemble_matrix_petsc(A, cpp_form, form_consts, form_coeffs, cpp_dirichletbc(bcs), False)

    # General assembly data
    block_size = dofmap.dof_layout.block_size()
    num_dofs_per_element = dofmap.dof_layout.num_dofs

    tdim = V.mesh.topology.dim

    # Assemble over cells
    subdomain_ids = cpp_form.integral_ids(_fem.IntegralType.cell)
    num_cell_integrals = len(subdomain_ids)

    e0 = cpp_form.function_spaces[0].element
    e1 = cpp_form.function_spaces[1].element

    # Get dof transformations
    needs_transformation_data = e0.needs_dof_transformations or e1.needs_dof_transformations or \
        cpp_form.needs_facet_permutations
    cell_perms = numpy.array([], dtype=numpy.uint32)
    if needs_transformation_data:
        V.mesh.topology.create_entity_permutations()
        cell_perms = V.mesh.topology.get_cell_permutation_info()
    # FIXME: Here we need to add the apply_dof_transformation and apply_dof_transformation transpose functions
    # to support more exotic elements
    if e0.needs_dof_transformations or e1.needs_dof_transformations:
        raise NotImplementedError("Dof transformations not implemented")

    nptype = "complex128" if is_complex else "float64"
    if num_cell_integrals > 0:
        V.mesh.topology.create_entity_permutations()
        for i, id in enumerate(subdomain_ids):
            coeffs_i = form_coeffs[(_fem.IntegralType.cell, id)]
            cell_kernel = getattr(ufc_form.integrals(_fem.IntegralType.cell)[i], f"tabulate_tensor_{nptype}")
            active_cells = cpp_form.domains(_fem.IntegralType.cell, id)
            assemble_slave_cells(A.handle, cell_kernel, active_cells[numpy.isin(active_cells, slave_cells)],
                                 (pos, x_dofs, x), coeffs_i, form_consts, cell_perms, dofs,
                                 block_size, num_dofs_per_element, mpc_data, is_bc)

    # Assemble over exterior facets
    subdomain_ids = cpp_form.integral_ids(_fem.IntegralType.exterior_facet)
    num_exterior_integrals = len(subdomain_ids)

    if num_exterior_integrals > 0:
        V.mesh.topology.create_entities(tdim - 1)
        V.mesh.topology.create_connectivity(tdim - 1, tdim)

        # Get facet permutations if required
        facet_perms = numpy.array([], dtype=numpy.uint8)

        if cpp_form.needs_facet_permutations:
            facet_perms = V.mesh.topology.get_facet_permutations()
        perm = (cell_perms, cpp_form.needs_facet_permutations, facet_perms)

        for i, id in enumerate(subdomain_ids):
            facet_kernel = getattr(ufc_form.integrals(_fem.IntegralType.exterior_facet)
                                   [i], f"tabulate_tensor_{nptype}")
            facets = cpp_form.domains(_fem.IntegralType.exterior_facet, id)
            coeffs_i = form_coeffs[(_fem.IntegralType.exterior_facet, id)]
            facet_info = pack_slave_facet_info(facets, slave_cells)
            num_facets_per_cell = len(V.mesh.topology.connectivity(tdim, tdim - 1).links(0))
            assemble_exterior_slave_facets(A.handle, facet_kernel, (pos, x_dofs, x), coeffs_i, form_consts,
                                           perm, dofs, block_size, num_dofs_per_element, facet_info, mpc_data, is_bc,
                                           num_facets_per_cell)

    # Add mpc entries on diagonal
    slaves = constraint.slaves
    num_local_slaves = constraint.num_local_slaves
    add_diagonal(A.handle, slaves[:num_local_slaves], diagval)

    # Add one on diagonal for diriclet bc and slave dofs
    # NOTE: In the future one could use a constant in the DirichletBC
    if cpp_form.function_spaces[0].id == cpp_form.function_spaces[1].id:
        A.assemblyBegin(_PETSc.Mat.AssemblyType.FLUSH)
        A.assemblyEnd(_PETSc.Mat.AssemblyType.FLUSH)
        _cpp.fem.insert_diagonal(A, cpp_form.function_spaces[0], cpp_dirichletbc(bcs), diagval)

    A.assemble()
    timer_matrix.stop()
    return A


@numba.jit
def add_diagonal(A: int, dofs: 'numpy.ndarray[numpy.int32]', diagval: _PETSc.ScalarType = 1):
    """
    Insert value on diagonal of matrix for given dofs.
    """
    ffi_fb = ffi.from_buffer
    dof_list = numpy.zeros(1, dtype=numpy.int32)
    dof_value = numpy.full(1, diagval, dtype=_PETSc.ScalarType)
    for dof in dofs:
        dof_list[0] = dof
        ierr_loc = set_values_local(A, 1, ffi_fb(dof_list), 1, ffi_fb(dof_list), ffi_fb(dof_value), mode)
        assert(ierr_loc == 0)
    sink(dof_list, dof_value)


@numba.njit
def assemble_slave_cells(A: int,
                         kernel: ffi.CData,
                         active_cells: 'numpy.ndarray[numpy.int32]',
                         mesh: Tuple['numpy.ndarray[numpy.int32]', 'numpy.ndarray[numpy.int32]',
                                     'numpy.ndarray[numpy.float64]'],
                         coeffs: 'numpy.ndarray[_PETSc.ScalarType]',
                         constants: 'numpy.ndarray[_PETSc.ScalarType]',
                         permutation_info: 'numpy.ndarray[numpy.uint32]',
                         dofmap: 'numpy.ndarray[numpy.int32]',
                         block_size: int,
                         num_dofs_per_element: int,
                         mpc: Tuple['numpy.ndarray[numpy.int32]', 'numpy.ndarray[_PETSc.ScalarType]',
                                    'numpy.ndarray[numpy.int32]', 'numpy.ndarray[numpy.int32]',
                                    'numpy.ndarray[numpy.int32]', 'numpy.ndarray[numpy.int32]'],
                         is_bc: 'numpy.ndarray[numpy.bool_]'):
    """
    Assemble MPC contributions for cell integrals
    """
    ffi_fb = ffi.from_buffer

    # Get mesh and geometry data
    pos, x_dofmap, x = mesh

    # Empty arrays mimicking Nullpointers
    facet_index = numpy.zeros(0, dtype=numpy.intc)
    facet_perm = numpy.zeros(0, dtype=numpy.uint8)

    # NOTE: All cells are assumed to be of the same type
    geometry = numpy.zeros((pos[1] - pos[0], 3))

    A_local = numpy.zeros((block_size * num_dofs_per_element, block_size
                           * num_dofs_per_element), dtype=_PETSc.ScalarType)
    masters, coefficients, offsets, c_to_s, c_to_s_off, is_slave = mpc

    # Loop over all cells
    local_dofs = numpy.zeros(block_size * num_dofs_per_element, dtype=numpy.int32)
    for cell in active_cells:
        num_vertices = pos[cell + 1] - pos[cell]
        geom_dofs = pos[cell]

        # Compute vertices of cell from mesh data
        geometry[:, :] = x[x_dofmap[geom_dofs:geom_dofs + num_vertices]]

        # Assemble local contributions
        A_local.fill(0.0)
        kernel(ffi_fb(A_local), ffi_fb(coeffs[cell, :]), ffi_fb(constants), ffi_fb(geometry),
               ffi_fb(facet_index), ffi_fb(facet_perm))

        # FIXME: Here we need to apply dof transformations

        # Local dof position
        local_blocks = dofmap[num_dofs_per_element
                              * cell: num_dofs_per_element * cell + num_dofs_per_element]

        # Remove all contributions for dofs that are in the Dirichlet bcs
        for j in range(num_dofs_per_element):
            for k in range(block_size):
                if is_bc[local_blocks[j] * block_size + k]:
                    A_local[j * block_size + k, :] = 0
                    A_local[:, j * block_size + k] = 0

        A_local_copy = A_local.copy()

        # Find local position of slaves
        slaves = c_to_s[c_to_s_off[cell]: c_to_s_off[cell + 1]]
        mpc_cell = (slaves, masters, coefficients, offsets, is_slave)
        modify_mpc_cell(A, num_dofs_per_element, block_size, A_local, local_blocks, mpc_cell)

        # Remove already assembled contribution to matrix
        A_contribution = A_local - A_local_copy

        # Expand local blocks to dofs
        for i in range(num_dofs_per_element):
            for j in range(block_size):
                local_dofs[i * block_size + j] = local_blocks[i] * block_size + j

        # Insert local contribution
        ierr_loc = set_values_local(A, block_size * num_dofs_per_element, ffi_fb(local_dofs),
                                    block_size * num_dofs_per_element, ffi_fb(local_dofs), ffi_fb(A_contribution), mode)
        assert(ierr_loc == 0)

    sink(A_contribution, local_dofs)


@numba.njit
def modify_mpc_cell(A: int, num_dofs: int, block_size: int,
                    Ae: 'numpy.ndarray[_PETSc.ScalarType]',
                    local_blocks: 'numpy.ndarray[numpy.int32]',
                    mpc_cell: Tuple['numpy.ndarray[numpy.int32]', 'numpy.ndarray[numpy.int32]',
                                    'numpy.ndarray[_PETSc.ScalarType]', 'numpy.ndarray[numpy.int32]',
                                    'numpy.ndarray[numpy.int8]']):
    """
    Given an element matrix Ae, modify the contributions to respect the MPCs, and add contributions to appropriate
    places in the global matrix A.
    """
    slaves, masters, coefficients, offsets, is_slave = mpc_cell

    # Locate which local dofs are slave dofs and compute the local index of the slave
    # Additionally count the number of masters we will needed in the flattened structures
    local_index0 = numpy.empty(len(slaves), dtype=numpy.int32)
    num_flattened_masters = 0
    for i in range(num_dofs):
        for j in range(block_size):
            slave = local_blocks[i] * block_size + j
            if is_slave[slave]:
                location = numpy.flatnonzero(slaves == slave)[0]
                local_index0[location] = i * block_size + j
                num_flattened_masters += offsets[slave + 1] - offsets[slave]
    # Strip a copy of Ae of all columns and rows belonging to a slave
    Ae_original = numpy.copy(Ae)
    Ae_stripped = numpy.zeros((block_size * num_dofs, block_size * num_dofs), dtype=_PETSc.ScalarType)
    for i in range(num_dofs):
        for b in range(block_size):
            is_slave0 = is_slave[local_blocks[i] * block_size + b]
            for j in range(num_dofs):
                for c in range(block_size):
                    is_slave1 = is_slave[local_blocks[j] * block_size + c]
                    Ae_stripped[i * block_size + b, j * block_size
                                + c] = (not (is_slave0 and is_slave1)) * Ae_original[i * block_size + b,
                                                                                     j * block_size + c]
    flattened_masters = numpy.zeros(num_flattened_masters, dtype=numpy.int32)
    flattened_slaves = numpy.zeros(num_flattened_masters, dtype=numpy.int32)
    flattened_coeffs = numpy.zeros(num_flattened_masters, dtype=_PETSc.ScalarType)
    c = 0
    for i, slave in enumerate(slaves):
        local_masters = masters[offsets[slave]: offsets[slave + 1]]
        local_coeffs = coefficients[offsets[slave]: offsets[slave + 1]]
        num_masters = len(local_masters)
        for j in range(num_masters):
            flattened_slaves[c + j] = local_index0[i]
            flattened_masters[c + j] = local_masters[j]
            flattened_coeffs[c + j] = local_coeffs[j]
        c += num_masters
    m0 = numpy.zeros(1, dtype=numpy.int32)
    m1 = numpy.zeros(1, dtype=numpy.int32)
    Am0m1 = numpy.zeros((1, 1), dtype=_PETSc.ScalarType)
    Arow = numpy.zeros((block_size * num_dofs, 1), dtype=_PETSc.ScalarType)
    Acol = numpy.zeros((1, block_size * num_dofs), dtype=_PETSc.ScalarType)
    mpc_dofs = numpy.zeros(block_size * num_dofs, dtype=numpy.int32)

    ffi_fb = ffi.from_buffer
    for i in range(num_flattened_masters):
        local_index = flattened_slaves[i]
        master = flattened_masters[i]
        coeff = flattened_coeffs[i]
        Ae[:, local_index] = 0
        Ae[local_index, :] = 0
        m0[0] = master
        Arow[:, 0] = coeff * Ae_stripped[:, local_index]
        Acol[0, :] = coeff * Ae_stripped[local_index, :]
        Am0m1[0, 0] = coeff**2 * Ae_original[local_index, local_index]
        for j in range(num_dofs):
            for k in range(block_size):
                mpc_dofs[j * block_size + k] = local_blocks[j] * block_size + k
        mpc_dofs[local_index] = master
        ierr_row = set_values_local(A, block_size * num_dofs, ffi_fb(mpc_dofs), 1, ffi_fb(m0), ffi_fb(Arow), mode)
        assert(ierr_row == 0)

        # Add slave row to master row
        ierr_col = set_values_local(A, 1, ffi_fb(m0), block_size * num_dofs, ffi_fb(mpc_dofs), ffi_fb(Acol), mode)
        assert(ierr_col == 0)

        ierr_master = set_values_local(A, 1, ffi_fb(m0), 1, ffi_fb(m0), ffi_fb(Am0m1), mode)
        assert(ierr_master == 0)

        # Add contributions for other masters relating to slaves on the given cell
        for j in range(num_flattened_masters):
            if i == j:
                continue
            other_local_index = flattened_slaves[j]
            other_master = flattened_masters[j]
            other_coeff = flattened_coeffs[j]
            m1[0] = other_master
            Am0m1[0, 0] = coeff * other_coeff * Ae_original[local_index, other_local_index]
            ierr_other_masters = set_values_local(A, 1, ffi_fb(m0), 1, ffi_fb(m1), ffi_fb(Am0m1), mode)
            assert(ierr_other_masters == 0)

    sink(Arow, Acol, Am0m1, m0, m1, mpc_dofs)


@numba.njit
def assemble_exterior_slave_facets(A: int, kernel: ffi.CData,
                                   mesh: Tuple['numpy.ndarray[numpy.int32]', 'numpy.ndarray[numpy.int32]',
                                               'numpy.ndarray[numpy.float64]'],
                                   coeffs: 'numpy.ndarray[_PETSc.ScalarType]',
                                   consts: 'numpy.ndarray[_PETSc.ScalarType]',
                                   perm: 'numpy.ndarray[numpy.uint32]',
                                   dofmap: 'numpy.ndarray[numpy.int32]',
                                   block_size: int,
                                   num_dofs_per_element: int,
                                   facet_info: 'numpy.ndarray[numpy.int32]',
                                   mpc: Tuple['numpy.ndarray[numpy.int32]', 'numpy.ndarray[_PETSc.ScalarType]',
                                              'numpy.ndarray[numpy.int32]', 'numpy.ndarray[numpy.int32]',
                                              'numpy.ndarray[numpy.int32]', 'numpy.ndarray[numpy.int32]'],
                                   is_bc: 'numpy.ndarray[numpy.bool_]',
                                   num_facets_per_cell: int):
    """Assemble MPC contributions over exterior facet integrals"""
    # Unpack mpc data
    masters, coefficients, offsets, c_to_s, c_to_s_off, is_slave = mpc

    # Mesh data
    pos, x_dofmap, x = mesh

    # Empty arrays for facet information
    facet_index = numpy.zeros(1, dtype=numpy.int32)
    facet_perm = numpy.zeros(1, dtype=numpy.uint8)

    # NOTE: All cells are assumed to be of the same type
    geometry = numpy.zeros((pos[1] - pos[0], 3))

    # Numpy data used in facet loop
    A_local = numpy.zeros((num_dofs_per_element * block_size,
                           num_dofs_per_element * block_size), dtype=_PETSc.ScalarType)
    local_dofs = numpy.zeros(block_size * num_dofs_per_element, dtype=numpy.int32)

    # Permutation info
    cell_perms, needs_facet_perm, facet_perms = perm

    # Loop over all external facets that are active
    for i in range(facet_info.shape[0]):
        #  Get cell index (local to process) and facet index (local to cell)
        cell_index, local_facet = facet_info[i]
        # Get mesh geometry
        cell = pos[cell_index]
        facet_index[0] = local_facet
        num_vertices = pos[cell_index + 1] - pos[cell_index]
        geometry[:, :] = x[x_dofmap[cell:cell + num_vertices]]

        # Assemble local matrix
        A_local.fill(0.0)
        if needs_facet_perm:
            facet_perm[0] = facet_perms[cell_index * num_facets_per_cell + local_facet]
        kernel(ffi.from_buffer(A_local), ffi.from_buffer(coeffs[cell_index, :]), ffi.from_buffer(consts),
               ffi.from_buffer(geometry), ffi.from_buffer(facet_index), ffi.from_buffer(facet_perm))
        # FIXME: Here we need to add the apply_dof_transformation and apply_dof_transformation transpose functions

        # Extract local blocks of dofs
        block_pos = num_dofs_per_element * cell_index
        local_blocks = dofmap[block_pos: block_pos + num_dofs_per_element]

        # Remove all contributions for dofs that are in the Dirichlet bcs
        for j in range(num_dofs_per_element):
            for k in range(block_size):
                if is_bc[local_blocks[j] * block_size + k]:
                    A_local[j * block_size + k, :] = 0
                    A_local[:, j * block_size + k] = 0

        A_local_copy = A_local.copy()
        slaves = c_to_s[c_to_s_off[cell_index]: c_to_s_off[cell_index + 1]]
        mpc_cell = (slaves, masters, coefficients, offsets, is_slave)
        modify_mpc_cell(A, num_dofs_per_element, block_size, A_local, local_blocks, mpc_cell)

        # Remove already assembled contribution to matrix
        A_contribution = A_local - A_local_copy

        # Expand local blocks to dofs
        for i in range(num_dofs_per_element):
            for j in range(block_size):
                local_dofs[i * block_size + j] = local_blocks[i] * block_size + j

        # Insert local contribution
        ierr_loc = set_values_local(A, block_size * num_dofs_per_element, ffi.from_buffer(local_dofs),
                                    block_size * num_dofs_per_element, ffi.from_buffer(local_dofs),
                                    ffi.from_buffer(A_contribution), mode)
        assert(ierr_loc == 0)

    sink(A_contribution, local_dofs)