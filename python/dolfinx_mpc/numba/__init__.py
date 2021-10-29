# Copyright (C) 2021 Jørgen Schartum Dokken
#
# This file is part of DOLFINX_MPC
#
# SPDX-License-Identifier:    LGPL-3.0-or-later
"""Numba extension for dolfinx_mpc"""

# flake8: noqa


try:
    import numba
except:
    raise ModuleNotFoundError("Numba is required to use numba assembler")

from .assemble_matrix import assemble_matrix
from .assemble_vector import assemble_vector
