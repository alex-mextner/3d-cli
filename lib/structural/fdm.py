"""Transversely isotropic FDM material model: stiffness, orientation-dependent allowables, failure indices.

An FDM part is isotropic inside a layer and weak across layers, so it is transversely isotropic
about the build axis b. The card (lib/data/materials.yaml, via registries.materials) supplies

  E_p  in-plane modulus                       (e_modulus_mpa)
  E_z  across-layer modulus  = E_p * z_modulus_factor
  nu_p in-plane Poisson ratio; nu_pz = nu_p (strain across layers per in-plane strain), nu_zp by reciprocity
  G_p  = E_p / (2 (1 + nu_p));  G_z = E_z / (2 (1 + nu_p))   (interlayer shear, approximation)
  X    in-plane strength  = yield_mpa;  Z = X * layer_adhesion across layers
  S_p  in-plane shear strength = X / sqrt(3) (von Mises);  S_z = S_p * layer_adhesion

The failure check separates the two physical modes instead of one smeared quadratic criterion:

  in-plane  FI_p = sqrt(s1^2 - s1 s2 + s2^2) / X          (s1, s2: in-layer principal stresses; plane von Mises)
  layer     FI_z = sqrt((<sigma_n>/Z)^2 + (tau_n/S_z)^2)   (Hashin-type delamination: tension across the layer
                                                            plane plus shear on it; compression across layers
                                                            does not open them)
  SF = 1 / max(FI_p, FI_z)

Both are reported, so the report shows which direction governs. All stresses MPa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from materials import Material

_VOIGT = (
    (0, 0),
    (1, 1),
    (2, 2),
    (1, 2),
    (0, 2),
    (0, 1),
)  # xx yy zz yz xz xy (engineering shear)


@dataclass(frozen=True)
class FdmCard:
    """Resolved mechanical card of one material, MPa."""

    name: str
    e_p: float
    e_z: float
    nu_p: float
    nu_pz: float
    g_p: float
    g_z: float
    yield_p: float
    yield_z: float
    shear_p: float
    shear_z: float
    strain_limit_p: float
    strain_limit_z: float
    layer_adhesion: float
    density_g_cm3: float
    defaulted: tuple[str, ...]

    @property
    def nu_zp(self) -> float:
        return self.nu_pz * self.e_z / self.e_p

    @classmethod
    def from_material(cls, material: Material) -> FdmCard:
        e_p = material.e_modulus_mpa
        e_z = e_p * material.z_modulus_factor
        nu = material.poisson
        shear_p = material.yield_mpa / math.sqrt(3.0)
        strain_p = material.strain_limit_pct / 100.0
        card = cls(
            name=material.name,
            e_p=e_p,
            e_z=e_z,
            nu_p=nu,
            nu_pz=nu,
            g_p=e_p / (2.0 * (1.0 + nu)),
            g_z=e_z / (2.0 * (1.0 + nu)),
            yield_p=material.yield_mpa,
            yield_z=material.yield_mpa * material.layer_adhesion,
            shear_p=shear_p,
            shear_z=shear_p * material.layer_adhesion,
            strain_limit_p=strain_p,
            strain_limit_z=strain_p * material.layer_adhesion,
            layer_adhesion=material.layer_adhesion,
            density_g_cm3=material.density,
            defaulted=tuple(material.defaulted),
        )
        card.stiffness_voigt()  # raises if the card is not positive definite
        return card

    # ---- stiffness ----------------------------------------------------------------------------
    def compliance_voigt(self) -> np.ndarray:
        """6x6 compliance in the material frame (axes 1, 2 in the layer plane, 3 = build axis)."""
        s = np.zeros((6, 6))
        s[0, 0] = s[1, 1] = 1.0 / self.e_p
        s[2, 2] = 1.0 / self.e_z
        s[0, 1] = s[1, 0] = -self.nu_p / self.e_p
        s[0, 2] = s[2, 0] = s[1, 2] = s[2, 1] = -self.nu_zp / self.e_z
        s[3, 3] = s[4, 4] = 1.0 / self.g_z
        s[5, 5] = 1.0 / self.g_p
        return s

    def stiffness_voigt(self) -> np.ndarray:
        """6x6 stiffness in the material frame; raises ValueError when the card is not thermodynamically admissible."""
        s = self.compliance_voigt()
        if np.min(np.linalg.eigvalsh(s)) <= 0.0:
            raise ValueError(
                f"material {self.name}: the elastic constants give a non-positive-definite compliance"
            )
        return np.linalg.inv(s)  # type: ignore[no-any-return]

    def stiffness_tensor(self, build_axis: np.ndarray) -> np.ndarray:
        """Fourth-order stiffness C_ijkl (3,3,3,3) in the part frame for the given build axis."""
        q = _frame_from_axis(build_axis)  # columns e1, e2, b: material -> part
        c_mat = voigt_to_tensor4(self.stiffness_voigt())
        return np.einsum("ai,bj,ck,dl,ijkl->abcd", q, q, q, q, c_mat, optimize=True)  # type: ignore[no-any-return]

    def stiffness_voigt_part(self, build_axis: np.ndarray) -> np.ndarray:
        """6x6 stiffness (engineering shear) in the part frame."""
        return tensor4_to_voigt(self.stiffness_tensor(build_axis))

    # ---- directional properties ---------------------------------------------------------------
    def modulus_along(self, direction: np.ndarray, build_axis: np.ndarray) -> float:
        """Young's modulus along `direction` (a tensile fibre) for a transversely isotropic solid."""
        c2 = _cos2(direction, build_axis)
        s2 = 1.0 - c2
        inv = (
            c2**2 / self.e_z
            + s2**2 / self.e_p
            + c2 * s2 * (1.0 / self.g_z - 2.0 * self.nu_zp / self.e_z)
        )
        return 1.0 / inv

    def strength_along(self, direction: np.ndarray, build_axis: np.ndarray) -> float:
        """Normal-stress allowable along `direction`: ellipse interpolation between X (in-plane) and Z."""
        c2 = _cos2(direction, build_axis)
        return 1.0 / math.sqrt(c2 / self.yield_z**2 + (1.0 - c2) / self.yield_p**2)

    def allowables(self) -> dict[str, float]:
        return {
            "in_plane_mpa": self.yield_p,
            "across_layers_mpa": self.yield_z,
            "shear_in_plane_mpa": self.shear_p,
            "shear_across_layers_mpa": self.shear_z,
        }


# ---- tensor helpers ---------------------------------------------------------------------------
def voigt_to_tensor4(c: np.ndarray) -> np.ndarray:
    """6x6 Voigt stiffness with engineering shear strain -> C_ijkl."""
    t = np.zeros((3, 3, 3, 3))
    for a, (i, j) in enumerate(_VOIGT):
        for b, (k, ell) in enumerate(_VOIGT):
            for p, q in ((i, j), (j, i)):
                for r, s in ((k, ell), (ell, k)):
                    t[p, q, r, s] = c[a, b]
    return t


def tensor4_to_voigt(t: np.ndarray) -> np.ndarray:
    c = np.zeros((6, 6))
    for a, (i, j) in enumerate(_VOIGT):
        for b, (k, ell) in enumerate(_VOIGT):
            c[a, b] = t[i, j, k, ell]
    return c


def _frame_from_axis(axis: np.ndarray) -> np.ndarray:
    b = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(b)
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("build axis must be a non-zero finite vector")
    b = b / norm
    helper = np.array([1.0, 0.0, 0.0]) if abs(b[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = np.cross(b, helper)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(b, e1)
    return np.column_stack([e1, e2, b])


def _cos2(direction: np.ndarray, build_axis: np.ndarray) -> float:
    d = np.asarray(direction, dtype=float)
    b = np.asarray(build_axis, dtype=float)
    return float(np.clip((d @ b) ** 2 / ((d @ d) * (b @ b)), 0.0, 1.0))


# ---- failure indices --------------------------------------------------------------------------
def failure_fields(
    stress: np.ndarray,
    build_axis: np.ndarray,
    card: FdmCard,
    strength_scale: np.ndarray | float = 1.0,
) -> dict[str, Any]:
    """Per-point stress measures for an array of stress tensors (..., 3, 3) in MPa.

    `strength_scale` (broadcast against the leading shape) scales every allowable, used for the
    infill knock-down. Returns von_mises, sigma_n, tau_n, fi_inplane, fi_layer, sf.
    """
    b = np.asarray(build_axis, dtype=float)
    b = b / np.linalg.norm(b)
    s = np.asarray(stress, dtype=float)
    tr = np.trace(s, axis1=-2, axis2=-1)
    dev = s - tr[..., None, None] / 3.0 * np.eye(3)
    von_mises = np.sqrt(1.5 * np.sum(dev * dev, axis=(-2, -1)))

    p = np.eye(3) - np.outer(b, b)
    s_ip = p @ s @ p
    f2 = np.sum(s_ip * s_ip, axis=(-2, -1))
    i1 = np.trace(s_ip, axis1=-2, axis2=-1)
    a1a2 = 0.5 * (i1**2 - f2)
    plane_vm = np.sqrt(np.maximum(f2 - a1a2, 0.0))

    traction = s @ b
    sigma_n = traction @ b
    tau_n = np.linalg.norm(traction - sigma_n[..., None] * b, axis=-1)

    scale = np.asarray(strength_scale, dtype=float)
    fi_p = plane_vm / (card.yield_p * scale)
    fi_z = np.sqrt(
        (np.maximum(sigma_n, 0.0) / (card.yield_z * scale)) ** 2
        + (tau_n / (card.shear_z * scale)) ** 2
    )
    worst = np.maximum(fi_p, fi_z)
    sf = np.where(worst > 0.0, 1.0 / np.where(worst > 0.0, worst, 1.0), np.inf)
    return {
        "von_mises": von_mises,
        "sigma_n": sigma_n,
        "tau_n": tau_n,
        "fi_inplane": fi_p,
        "fi_layer": fi_z,
        "sf": sf,
    }
