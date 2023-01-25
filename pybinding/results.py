"""Processing and presentation of computed data

Result objects hold computed data and offer postprocessing and plotting functions
which are specifically adapted to the nature of the stored data.
"""
from copy import copy

import numpy as np
import matplotlib.pyplot as plt
from numpy.typing import ArrayLike
from collections.abc import Iterable
from typing import Literal, Optional, Union, Tuple, List
import matplotlib
from matplotlib.patches import FancyArrow

from . import pltutils
from .utils import with_defaults, x_pi
from .support.pickle import pickleable, save, load
from .support.structure import Positions, AbstractSites, Sites, Hoppings
from .support.alias import AliasArray
from matplotlib.collections import LineCollection, PathCollection

__all__ = ['Bands', 'Path', 'Eigenvalues', 'NDSweep', 'Series', 'SpatialMap', 'StructureMap',
           'Sweep', 'make_path', 'save', 'load', 'Wavefunction', 'Disentangle', 'FatBands',
           'SpatialLDOS']


def _make_crop_indices(obj, limits):
    # TODO add typing--> can't add Structure or SpatialMap due to picklable
    """Return the indices into `obj` which retain only the data within the given limits"""
    idx = np.ones(obj.num_sites, dtype=bool)
    for name, limit in limits.items():
        v = getattr(obj, name)
        idx = np.logical_and(idx, v >= limit[0])
        idx = np.logical_and(idx, v < limit[1])
    return idx


class Path(np.ndarray):
    """A ndarray which represents a path connecting certain points

    Attributes
    ----------
    point_indices : List[int]
        Indices of the significant points along the path. Minimum 2: start and end.
    point_labels : Optional[List[str]]
        Labels for the significant points along the path.
    """
    def __new__(cls, array: ArrayLike, point_indices: List[int], point_labels: Optional[List[str]] = None):
        obj = np.asarray(array).view(cls)
        assert len(point_indices) >= 2
        obj.point_indices = point_indices
        obj.point_labels = point_labels
        return obj

    def __array_finalize__(self, obj):
        if obj is None:
            return
        default_indices = [0, obj.shape[0] - 1] if len(obj.shape) >= 1 else []
        self.point_indices = getattr(obj, 'point_indices', default_indices)
        self.point_labels = getattr(obj, 'point_labels', [str(i) for i in default_indices])

    def __reduce__(self):
        r = super().__reduce__()
        state = r[2] + (self.point_indices, self.point_labels)
        return r[0], r[1], state

    # noinspection PyMethodOverriding,PyArgumentList
    def __setstate__(self, state):
        self.point_indices = state[-2]
        self.point_labels = state[-1]
        super().__setstate__(state[:-2])

    @property
    def points(self) -> np.ndarray:
        """Significant points along the path, including start and end"""
        return self[self.point_indices]

    @property
    def is_simple(self) -> bool:
        """Is it just a simple path between two points?"""
        return len(self.point_indices) == 2

    def as_1d(self) -> np.ndarray:
        """Return a 1D representation of the path -- useful for plotting

        For simple paths (2 points) the closest 1D path with real positions is returned.
        Otherwise, an `np.arange(size)` is returned, where `size` matches the path. This doesn't
        have any real meaning, but it's something that can be used as the x-axis in a line plot.

        Examples
        --------
        >>> np.allclose(make_path(-2, 1, step=1).as_1d().T, [-2, -1, 0, 1])
        True
        >>> np.allclose(make_path([0, -2], [0, 1], step=1).as_1d().T, [-2, -1, 0, 1])
        True
        >>> np.allclose(make_path(1, -1, 4, step=1).as_1d().T, [0, 1, 2, 3, 4, 5, 6, 7])
        True
        """
        if self.is_simple:
            if len(self.shape) == 1:
                return self
            else:  # return the first axis with non-zero length
                return self[:, np.flatnonzero(np.diff(self.points, axis=0))[0]]
        else:
            return np.append([0], np.sqrt((np.diff(self, axis=0) ** 2).dot(np.ones((self.shape[1], 1)))).cumsum())

    def plot(self, point_labels: Optional[List[str]] = None, ax: Optional[plt.Axes] = None,
             **kwargs) -> FancyArrow:
        """Quiver plot of the path

        Parameters
        ----------
        point_labels : List[str]
            Labels for the :attr:`.Path.points`.
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Forwarded to :func:`~matplotlib.pyplot.arrow`.
        """
        if ax is None:
            ax = plt.gca()
        ax.set_aspect('equal')
        # TODO: plot in 3D
        default_color = pltutils.get_palette('Set1')[1]
        kwargs = with_defaults(kwargs, scale=1, zorder=2, lw=1.5, color=default_color,
                               name=None, head_width=0.08, head_length=0.2)

        out = pltutils.plot_vectors(np.diff(self.points, axis=0), self.points[0:], ax=ax, **kwargs)

        ax.autoscale_view()
        pltutils.add_margin(0.5, ax=ax)
        pltutils.despine(trim=True, ax=ax)

        if point_labels is None:
            point_labels = self.point_labels

        if point_labels:
            for k_point, label in zip(self.points, point_labels):
                ha, va = pltutils.align(*(-k_point))
                pltutils.annotate_box(label, k_point * 1.05, fontsize='large',
                                      ha=ha, va=va, bbox=dict(lw=0), ax=ax)
        return out


def make_path(k0: ArrayLike, k1: ArrayLike, *ks: Iterable[ArrayLike], step: float = 0.1,
              point_labels: Optional[List[str]] = None) -> Path:
    """Create a path which connects the given k points

    Parameters
    ----------
    k0, k1, *ks
        Points in k-space to connect.
    step : float
        Length in k-space between two samples. Smaller step -> finer detail.
    point_labels : Optional[List[str]]
        The labels for the points.

    Examples
    --------
    >>> np.allclose(make_path(0, 3, -1, step=1).T, [0, 1, 2, 3, 2, 1, 0, -1])
    True
    >>> np.allclose(make_path([0, 0], [2, 3], [-1, 4], step=1.4),
    ...             [[0, 0], [1, 1.5], [2, 3], [0.5, 3.5], [-1, 4]])
    True
    """
    k_points = [np.atleast_1d(k) for k in (k0, k1) + ks]
    if not all(k.shape == k_points[0].shape for k in k_points[:1]):
        raise RuntimeError("All k-points must have the same shape")

    k_paths = []
    point_indices = [0]
    for k_start, k_end in zip(k_points[:-1], k_points[1:]):
        num_steps = int(np.linalg.norm(k_end - k_start) // step)
        # k_path.shape == num_steps, k_space_dimensions
        k_path = np.array([np.linspace(s, e, num_steps, endpoint=False)
                           for s, e in zip(k_start, k_end)]).T
        k_paths.append(k_path)
        point_indices.append(point_indices[-1] + num_steps)
    k_paths.append(k_points[-1])

    return Path(np.vstack(k_paths), point_indices, point_labels)


@pickleable
class Series:
    """A series of data points determined by a common relation, i.e. :math:`y = f(x)`

    Attributes
    ----------
    variable : array_like
        Independent variable for which the data was computed.
    data : array_like
        An array of values which were computed as a function of `variable`.
        It can be 1D or 2D. In the latter case each column represents the result
        of a different function applied to the same `variable` input.
    labels : dict
        Plot labels: 'variable', 'data', 'orbitals', 'title' and 'columns'.
    """
    def __init__(self, variable: ArrayLike, data: ArrayLike, labels: Optional[dict] = None):
        self.variable = np.atleast_1d(variable)
        self.data = np.atleast_1d(data)
        self.labels = with_defaults(
            labels, variable="x", data="y", columns="", title="",
            orbitals=[str(i) for i in range(self.data.shape[1])] if self.data.ndim == 2 else [])

    def with_data(self, data: np.ndarray) -> 'Series':
        """Return a copy of this result object with different data"""
        result = copy(self)
        result.data = data
        return result

    def __add__(self, other: 'Series') -> 'Series':
        """Add together the data of two Series object in a new object."""
        if self.data.ndim < other.data.ndim:
            # keep information about the orbitals, so take the other series as a reference
            return other.with_data(self.data[:, np.newaxis] + other.data)
        elif self.data.ndim > other.data.ndim:
            return self.with_data(self.data + other.data[:, np.newaxis])
        else:
            return self.with_data(self.data + other.data)

    def __sub__(self, other: 'Series') -> 'Series':
        """Subtract the data of two Series object in a new object."""
        if self.data.ndim < other.data.ndim:
            # keep information about the orbitals, so take the other series as a reference
            return other.with_data(self.data[:, np.newaxis] - other.data)
        elif self.data.ndim > other.data.ndim:
            return self.with_data(self.data - other.data[:, np.newaxis])
        else:
            return self.with_data(self.data - other.data)

    def reduced(self, columns: Optional[List[int]] = None, orbitals: Optional[List[str]] = None,
                fill_other: float = 0.) -> 'Series':
        """Return a copy where the data is summed over the columns

        Only applies to results which may have multiple columns of data, e.g.
        results for multiple orbitals for LDOS calculation.

        Parameters
        ----------
        columns : Optional[List[int]]
            The colummns to contract to the new array.
            The length of `columns` agrees with the dimensions of data.shape[1].
            The value at each position corresponds to the new column of the new Series object
        orbitals: Optional[List[str]]
            Optional new list of entries for the `orbitals` label in `labels`
        fill_other : float
            In case an array is made with a new column, fill it with this value. Default: 0.
        """
        col_idx = np.array(columns or np.zeros(self.data.shape[1]), dtype=int)
        if np.all(col_idx == 0):
            # case where all the axis are summed over, no 'orbital' label is needed
            return self.with_data(self.data.sum(axis=1))
        col_max = np.max(col_idx) + 1
        if orbitals is None:
            orb_list = [str(i) for i in range(col_max)]
            for c_i in np.unique(col_idx):
                orb_list[c_i] = self.labels["orbitals"][np.argmax(col_idx == c_i)]
        else:
            orb_list = orbitals
        data = np.full((self.data.shape[0], col_max), fill_other)
        for c_i in np.unique(col_idx):
            data[:, c_i] = np.sum(self.data[:, col_idx == c_i], axis=1)
        series_out = self.with_data(data)
        series_out.labels["orbitals"] = orb_list
        return series_out

    def plot(self, ax: Optional[plt.Axes] = None, axes: Literal['xy', 'yx'] = 'xy', **kwargs) -> None:
        """Labeled line plot

        Parameters
        ----------
        ax : Optional[plt.Axes]
            The Axis to plot the results on.
        axes : Literal['xy', 'yx']
            The order of the axes, default: 'xy'.
        **kwargs
            Forwarded to `plt.plot()`.
        """
        if ax is None:
            ax = plt.gca()
        if axes == "xy":
            ax.plot(self.variable, self.data, **kwargs)
            ax.set_xlim(self.variable.min(), self.variable.max())
            ax.set_xlabel(self.labels["variable"])
            ax.set_ylabel(self.labels["data"])
        elif axes == "yx":
            ax.plot(self.data, self.variable, **kwargs)
            ax.set_ylim(self.variable.min(), self.variable.max())
            ax.set_xlabel(self.labels["data"])
            ax.set_ylabel(self.labels["variable"])

        if "title" in self.labels:
            ax.set_title(self.labels["title"])
        pltutils.despine(ax=ax)

        if self.data.ndim > 1:
            labels = [str(i) for i in range(self.data.shape[-1])]
            if "orbitals" in self.labels:
                labels = self.labels["orbitals"]
            pltutils.legend(labels=labels, title=self.labels["columns"], ax=ax)


@pickleable
class SpatialMap:
    """Represents some spatially dependent property: data mapped to site positions"""
    # TODO: check typing
    def __init__(self, data: ArrayLike, positions: Union[ArrayLike, AbstractSites], sublattices=None):
        self._data = np.atleast_1d(data)
        if sublattices is None and isinstance(positions, AbstractSites):
            self._sites = positions
        else:
            self._sites = Sites(positions, sublattices)

        if self.num_sites != data.size:
            raise RuntimeError("Data size doesn't match number of sites")

    @property
    def num_sites(self) -> int:
        """Total number of lattice sites"""
        return self._sites.size

    @property
    def data(self) -> np.ndarray:
        """1D array of values for each site, i.e. maps directly to x, y, z site coordinates"""
        return self._data

    @data.setter
    def data(self, value: ArrayLike):
        self._data = value

    @property
    def positions(self) -> Positions:
        """Lattice site positions. Named tuple with x, y, z fields, each a 1D array."""
        return self._sites.positions

    @property
    def xyz(self) -> np.ndarray:
        """Return a new array with shape=(N, 3). Convenient, but slow for big systems."""
        return np.array(self.positions).T

    @property
    def x(self) -> np.ndarray:
        """1D array of coordinates, short for :attr:`.positions.x <.SpatialMap.positions.x>`"""
        return self._sites.x

    @property
    def y(self) -> np.ndarray:
        """1D array of coordinates, short for :attr:`.positions.y <.SpatialMap.positions.y>`"""
        return self._sites.y

    @property
    def z(self) -> np.ndarray:
        """1D array of coordinates, short for :attr:`.positions.z <.SpatialMap.positions.z>`"""
        return self._sites.z

    @property
    def sublattices(self) -> np.ndarray:
        """1D array of sublattices IDs"""
        return self._sites.ids

    @property
    def sub(self) -> np.ndarray:
        """1D array of sublattices IDs, short for :attr:`.sublattices <.SpatialMap.sublattices>`"""
        return self._sites.ids

    def with_data(self, data) -> "SpatialMap":
        """Return a copy of this object with different data mapped to the sites"""
        result = copy(self)
        result._data = data
        return result

    def save_txt(self, filename: str):
        with open(filename + '.dat', 'w') as file:
            file.write('# {:12}{:13}{:13}\n'.format('x(nm)', 'y(nm)', 'data'))
            for x, y, d in zip(self.x, self.y, self.data):
                file.write(("{:13.5e}" * 3 + '\n').format(x, y, d))

    def __getitem__(self, idx: Union[int, ArrayLike]):
        """Same rules as numpy indexing"""
        if hasattr(idx, "contains"):
            idx = idx.contains(*self.positions)  # got a Shape object -> evaluate it
        return self.__class__(self._data[idx], self._sites[idx])

    def cropped(self, **limits):
        # TODO: add typing
        """Return a copy which retains only the sites within the given limits

        Parameters
        ----------
        **limits
            Attribute names and corresponding limits. See example.

        Examples
        --------
        Leave only the data where -10 <= x < 10 and 2 <= y < 4::

            new = original.cropped(x=[-10, 10], y=[2, 4])
        """
        return self[_make_crop_indices(self, limits)]

    def clipped(self, v_min, v_max):
        # TODO: add typing
        """Clip (limit) the values in the `data` array, see :func:`~numpy.clip`"""
        return self.with_data(np.clip(self.data, v_min, v_max))

    def convolve(self, sigma: float = 0.25) -> np.ndarray:
        # TODO: slow and only works in the xy-plane
        x, y, _ = self.positions
        r = np.sqrt(x**2 + y**2)

        data = np.empty_like(self.data)
        for i in range(len(data)):
            idx = np.abs(r - r[i]) < sigma
            data[i] = np.sum(self.data[idx] * np.exp(-0.5 * ((r[i] - r[idx]) / sigma)**2))
            data[i] /= np.sum(np.exp(-0.5 * ((r[i] - r[idx]) / sigma)**2))

        self._data = data

    @staticmethod
    def _decorate_plot(ax: Optional[plt.Axes] = None):
        if ax is None:
            ax = plt.gca()
        ax.set_aspect('equal')
        ax.set_xlabel('x (nm)')
        ax.set_ylabel('y (nm)')
        pltutils.despine(trim=True, ax=ax)

    def plot_pcolor(self, ax: Optional[plt.Axes] = None, **kwargs):
        # TODO: add typing
        """Color plot of the xy plane

        Parameters
        ----------
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Forwarded to :func:`~matplotlib.pyplot.tripcolor`.
        """
        if ax is None:
            ax = plt.gca()
        x, y, _ = self.positions
        kwargs = with_defaults(kwargs, shading='gouraud', rasterized=True)
        pcolor = ax.tripcolor(x, y, self.data, **kwargs)
        self._decorate_plot(ax=ax)
        return pcolor

    def plot_contourf(self, num_levels: int = 50, ax: Optional[plt.Axes] = None, **kwargs):
        # TODO: add typing
        """Filled contour plot of the xy plane

        Parameters
        ----------
        num_levels : int
            Number of contour levels.
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Forwarded to :func:`~matplotlib.pyplot.tricontourf`.
        """
        if ax is None:
            ax = plt.gca()
        levels = np.linspace(self.data.min(), self.data.max(), num=num_levels)
        x, y, _ = self.positions
        kwargs = with_defaults(kwargs, levels=levels)
        contourf = ax.tricontourf(x, y, self.data, **kwargs)
        # Each collection has to be rasterized, `tricontourf()` does not accept `rasterized=True`
        for collection in contourf.collections:
            collection.set_rasterized(True)
        self._decorate_plot(ax=ax)
        return contourf

    def plot_contour(self, ax: Optional[plt.Axes] = None, **kwargs):
        # TODO: add typing
        """Contour plot of the xy plane

        Parameters
        ----------
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Forwarded to :func:`~matplotlib.pyplot.tricontour`.
        """
        if ax is None:
            ax = plt.gca()
        x, y, _ = self.positions
        contour = ax.tricontour(x, y, self.data, **kwargs)
        self._decorate_plot(ax=ax)
        return contour


@pickleable
class StructureMap(SpatialMap):
    """A subclass of :class:`.SpatialMap` that also includes hoppings between sites"""

    def __init__(self, data: ArrayLike, sites: Sites, hoppings: Hoppings, boundaries=()):
        # TODO: add typing
        super().__init__(data, sites)
        self._hoppings = hoppings
        self._boundaries = boundaries

    @property
    def spatial_map(self) -> SpatialMap:
        """Just the :class:`SpatialMap` subset without hoppings"""
        return SpatialMap(self._data, self._sites)

    @property
    def hoppings(self) -> Hoppings:
        """Sparse matrix of hopping IDs"""
        return self._hoppings

    @property
    def boundaries(self) -> list:
        """Boundary hoppings between different translation units (only for infinite systems)"""
        return self._boundaries

    def __getitem__(self, idx: int or list[int]) -> 'StructureMap':
        """Same rules as numpy indexing"""
        if hasattr(idx, "contains"):
            idx = idx.contains(*self.positions)  # got a Shape object -> evaluate it
        return self.__class__(self.data[idx], self._sites[idx], self._hoppings[idx],
                              [b[idx] for b in self._boundaries])

    def with_data(self, data) -> "StructureMap":
        """Return a copy of this object with different data mapped to the sites"""
        result = copy(self)
        result._data = data
        return result

    def plot(self, cmap: str = 'YlGnBu', site_radius: tuple[float, float] = (0.03, 0.05), num_periods: int = 1,
             ax: Optional[plt.Axes] = None, **kwargs) -> Optional[matplotlib.collections.CircleCollection]:
        # TODO: add typing
        """Plot the spatial structure with a colormap of :attr:`data` at the lattice sites

        Both the site size and color are used to display the data.

        Parameters
        ----------
        cmap : str
            Matplotlib colormap to be used for the data.
        site_radius : Tuple[float, float]
            Min and max radius of lattice sites. This range will be used to visually
            represent the magnitude of the data.
        num_periods : int
            Number of times to repeat periodic boundaries.
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Additional plot arguments as specified in :func:`.structure_plot_properties`.
        """
        if ax is None:
            ax = plt.gca()
        from .system import (plot_sites, plot_hoppings, plot_periodic_boundaries,
                             structure_plot_properties, decorate_structure_plot)

        def to_radii(data: np.ndarray) -> Union[float, tuple, list]:
            if not isinstance(site_radius, (tuple, list)):
                return site_radius

            positive_data = data - data.min()
            maximum = positive_data.max()
            if not np.allclose(maximum, 0):
                delta = site_radius[1] - site_radius[0]
                return site_radius[0] + delta * positive_data / maximum
            else:
                return site_radius[1]

        props = structure_plot_properties(**kwargs)
        props['site'] = with_defaults(props['site'], radius=to_radii(self.data), cmap=cmap)
        collection = plot_sites(self.positions, self.data, **props['site'], ax=ax)

        hop = self.hoppings.tocoo()
        props['hopping'] = with_defaults(props['hopping'], color='#bbbbbb')
        plot_hoppings(self.positions, hop, **props['hopping'], ax=ax)

        props['site']['alpha'] = props['hopping']['alpha'] = 0.5
        plot_periodic_boundaries(self.positions, hop, self.boundaries, self.data,
                                 num_periods, **props, ax=ax)

        decorate_structure_plot(**props, ax=ax)

        if collection:
            plt.sci(collection)
        return collection


@pickleable
class Structure:
    """Holds and plots the structure of a tight-binding system
    
    Similar to :class:`StructureMap`, but only holds the structure without 
    mapping to any actual data.
    """
    def __init__(self, sites: Union[Sites, '_CppSites'], hoppings: Hoppings, boundaries=()):
        # TODO: add typing
        self._sites = sites
        self._hoppings = hoppings
        self._boundaries = boundaries

    @property
    def num_sites(self) -> int:
        """Total number of lattice sites"""
        return self._sites.size

    @property
    def positions(self) -> Positions:
        """Lattice site positions. Named tuple with x, y, z fields, each a 1D array."""
        return self._sites.positions

    @property
    def xyz(self) -> np.ndarray:
        """Return a new array with shape=(N, 3). Convenient, but slow for big systems."""
        return np.array(self.positions).T

    @property
    def x(self) -> np.ndarray:
        """1D array of coordinates, short for :attr:`.positions.x <.SpatialMap.positions.x>`"""
        return self._sites.x

    @property
    def y(self) -> np.ndarray:
        """1D array of coordinates, short for :attr:`.positions.y <.SpatialMap.positions.y>`"""
        return self._sites.y

    @property
    def z(self) -> np.ndarray:
        """1D array of coordinates, short for :attr:`.positions.z <.SpatialMap.positions.z>`"""
        return self._sites.z

    @property
    def sublattices(self) -> np.ndarray:
        """1D array of sublattices IDs"""
        return self._sites.ids

    @property
    def sub(self) -> np.ndarray:
        """1D array of sublattices IDs, short for :attr:`.sublattices <.SpatialMap.sublattices>`"""
        return self._sites.ids

    @property
    def hoppings(self) -> Hoppings:
        """Sparse matrix of hopping IDs"""
        return self._hoppings

    @property
    def boundaries(self) -> list:
        """Boundary hoppings between different translation units (only for infinite systems)"""
        return self._boundaries

    def __getitem__(self, idx: Union[int, list[int]]) -> 'Structure':
        """Same rules as numpy indexing"""
        if hasattr(idx, "contains"):
            idx = idx.contains(*self.positions)  # got a Shape object -> evaluate it

        sliced = Structure(self._sites[idx], self._hoppings[idx],
                           [b[idx] for b in self._boundaries])
        if hasattr(self, "lattice"):
            sliced.lattice = self.lattice
        return sliced

    def find_nearest(self, position: ArrayLike, sublattice: str = "") -> int:
        """Find the index of the atom closest to the given position

        Parameters
        ----------
        position : array_like
            Where to look.
        sublattice : Optional[str]
            Look for a specific sublattice site. By default any will do.

        Returns
        -------
        int
        """
        return self._sites.find_nearest(position, sublattice)

    def cropped(self, **limits) -> 'Structure':
        """Return a copy which retains only the sites within the given limits

        Parameters
        ----------
        **limits
            Attribute names and corresponding limits. See example.

        Examples
        --------
        Leave only the data where -10 <= x < 10 and 2 <= y < 4::

            new = original.cropped(x=[-10, 10], y=[2, 4])
        """
        return self[_make_crop_indices(self, limits)]

    def with_data(self, data: ArrayLike) -> StructureMap:
        """Map some data to this structure"""
        return StructureMap(data, self._sites, self._hoppings, self._boundaries)

    def plot(self, num_periods: int = 1, ax: Optional[plt.Axes] = None,
             **kwargs) -> Optional[matplotlib.collections.CircleCollection]:
        """Plot the structure: sites, hoppings and periodic boundaries (if any)

        Parameters
        ----------
        num_periods : int
            Number of times to repeat the periodic boundaries.
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Additional plot arguments as specified in :func:`.structure_plot_properties`.
        """
        if ax is None:
            ax = plt.gca()
        from .system import (plot_sites, plot_hoppings, plot_periodic_boundaries,
                             structure_plot_properties, decorate_structure_plot)

        props = structure_plot_properties(**kwargs)
        if hasattr(self, "lattice"):
            props["site"].setdefault("radius", self.lattice.site_radius_for_plot())

        plot_hoppings(self.positions, self._hoppings, **props['hopping'], ax=ax)
        collection = plot_sites(self.positions, self.sublattices, **props['site'], ax=ax)
        plot_periodic_boundaries(self.positions, self._hoppings, self._boundaries,
                                 self.sublattices, num_periods, **props, ax=ax)

        decorate_structure_plot(**props, ax=ax)
        return collection


@pickleable
class Eigenvalues:
    """Hamiltonian eigenvalues with optional probability map

    Attributes
    ----------
    values : np.ndarray
    probability : np.ndarray
    """
    def __init__(self, eigenvalues: np.ndarray, probability: Optional[np.ndarray] = None):
        self.values = np.atleast_1d(eigenvalues)
        self.probability = np.atleast_1d(probability)

    @property
    def indices(self) -> np.ndarray:
        return np.arange(0, self.values.size)

    def _decorate_plot(self, mark_degenerate: bool, number_states: bool, margin: float = 0.1,
                       ax: Optional[plt.Axes] = None) -> None:
        """Common elements for the two eigenvalue plots"""
        if ax is None:
            ax = plt.gca()
        if mark_degenerate:
            # draw lines between degenerate states
            from .solver import Solver
            from matplotlib.collections import LineCollection
            pairs = ((s[0], s[-1]) for s in Solver.find_degenerate_states(self.values))
            lines = [[(i, self.values[i]) for i in pair] for pair in pairs]
            ax.add_collection(LineCollection(lines, color='black', alpha=0.5))

        if number_states:
            # draw a number next to each state
            for index, energy in enumerate(self.values):
                pltutils.annotate_box(index, (index, energy), fontsize='x-small',
                                      xytext=(0, -10), textcoords='offset points', ax=ax)
            margin = 0.25

        ax.set_xlabel('state')
        ax.set_ylabel('E (eV)')
        ax.set_xlim(-1, len(self.values))
        pltutils.despine(trim=True, ax=ax)
        pltutils.add_margin(margin, axis="y", ax=ax)

    def plot(self, mark_degenerate: bool = True, show_indices: bool = False, ax: Optional[plt.Axes] = None,
             **kwargs) -> matplotlib.collections.PathCollection:
        """Standard eigenvalues scatter plot

        Parameters
        ----------
        mark_degenerate : bool
            Plot a line which connects degenerate states.
        show_indices : bool
            Plot index number next to all states.
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Forwarded to plt.scatter().
        """
        if ax is None:
            ax = plt.gca()
        collection = ax.scatter(self.indices, self.values, **with_defaults(kwargs, c='#377ec8', s=15, lw=0.1))
        self._decorate_plot(mark_degenerate, show_indices, ax=ax)
        return collection

    def plot_heatmap(self, size: tuple[int, int] = (7, 77), mark_degenerate: bool = True, show_indices: bool = False,
                     ax: Optional[plt.Axes] = None, **kwargs) -> Optional[float]:
        """Eigenvalues scatter plot with a heatmap indicating probability density

        Parameters
        ----------
        size : Tuple[int, int]
            Min and max scatter dot size.
        mark_degenerate : bool
            Plot a line which connects degenerate states.
        show_indices : bool
            Plot index number next to all states.
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Forwarded to plt.scatter().
        """
        if ax is None:
            ax = plt.gca()
        if not np.any(self.probability):
            self.plot(mark_degenerate, show_indices, **kwargs, ax=ax)
            return 0

        # higher probability states should be drawn above lower ones
        idx = np.argsort(self.probability)
        indices, energy, probability = (v[idx] for v in
                                        (self.indices, self.values, self.probability))

        scatter_point_sizes = size[0] + size[1] * probability / probability.max()
        ax.scatter(indices, energy, **with_defaults(kwargs, cmap='YlOrRd', lw=0.2, alpha=0.85,
                                                    c=probability, s=scatter_point_sizes,
                                                    edgecolor="k"))

        self._decorate_plot(mark_degenerate, show_indices)
        return self.probability.max()


@pickleable
class Bands:
    """Band structure along a path in k-space

    Attributes
    ----------
    k_path : :class:`Path`
        The path in reciprocal space along which the bands were calculated.
        E.g. constructed using :func:`make_path`.
    energy : array_like
        Energy values for the bands along the path in k-space.
    """
    def __init__(self, k_path: Path, energy: np.ndarray):
        self.k_path: Path = np.atleast_1d(k_path).view(Path)
        self.energy = np.atleast_1d(energy)

    def _point_names(self, k_points: list[float]) -> list[str]:
        names = []
        if self.k_path.point_labels:
            return self.k_path.point_labels
        for k_point in k_points:
            k_point = np.atleast_1d(k_point)
            values = map(x_pi, k_point)
            fmt = "[{}]" if len(k_point) > 1 else "{}"
            names.append(fmt.format(', '.join(values)))
        return names

    @property
    def num_bands(self) -> int:
        return self.energy.shape[1]

    def plot(self, point_labels: Optional[List[str]] = None, ax: Optional[plt.Axes] = None,
             **kwargs) -> Optional[List[plt.Line2D]]:
        """Line plot of the band structure

        Parameters
        ----------
        point_labels : Optional[List[str]]
            Labels for the `k_points`.
        ax : Optional[plt.Axes]
            The Axis to plot the bands on.
        **kwargs
            Forwarded to `plt.plot()`.
        """
        if ax is None:
            ax = plt.gca()
        default_color = pltutils.get_palette('Set1')[1]
        default_linewidth = np.clip(5 / self.num_bands, 1.1, 1.6)
        kwargs = with_defaults(kwargs, color=default_color, lw=default_linewidth)

        k_space = self.k_path.as_1d()
        lines_out = ax.plot(k_space, self.energy, **kwargs)

        self._decorate_plot(point_labels, ax)
        return lines_out

    def _decorate_plot(self, point_labels: Optional[List[str]] = None, ax: Optional[plt.Axes] = None) -> None:
        """Decorate the band structure

        Parameters
        ----------
        point_labels : Optional[List[str]]
            Labels for the `k_points`.
        ax : Optional[plt.Axes]
            The Axis to plot the bands on.
        """
        if ax is None:
            ax = plt.gca()

        k_space = self.k_path.as_1d()

        ax.set_xlim(k_space.min(), k_space.max())
        ax.set_xlabel('k-space')
        ax.set_ylabel('E (eV)')
        pltutils.add_margin(ax=ax)
        pltutils.despine(trim=True, ax=ax)

        point_labels = point_labels or self._point_names(self.k_path.points)
        assert len(point_labels) == len(self.k_path.point_indices), \
            "The length of point_labels and point_indices aren't the same, len({0}) != len({1})".format(
                point_labels, self.k_path.point_indices
            )
        ax.set_xticks(k_space[self.k_path.point_indices], point_labels)

        # Draw vertical lines at significant points. Because of the `transLimits.transform`,
        # this must be the done last, after all others plot elements are positioned.
        for idx in self.k_path.point_indices:
            ymax = ax.transLimits.transform([0, np.nanmax(self.energy[idx])])[1]
            ax.axvline(k_space[idx], ymax=ymax, color="0.4", lw=0.8, ls=":", zorder=-1)

    def plot_kpath(self, point_labels: Optional[List[str]] = None, **kwargs) -> None:
        """Quiver plot of the k-path along which the bands were computed

        Combine with :meth:`.Lattice.plot_brillouin_zone` to see the path in context.

        Parameters
        ----------
        point_labels : Optional[List[str]]
            Labels for the k-points.
        **kwargs
            Forwarded to :func:`~matplotlib.pyplot.quiver`.
        """
        self.k_path.plot(point_labels, **kwargs)

    def dos(self, energies: Optional[ArrayLike] = None, broadening: Optional[float] = None) -> Series:
        r"""Calculate the density of states as a function of energy

        .. math::
            \text{DOS}(E) = \frac{1}{c \sqrt{2\pi}}
                            \sum_n{e^{-\frac{(E_n - E)^2}{2 c^2}}}

        for each :math:`E` in `energies`, where :math:`c` is `broadening` and
        :math:`E_n` is `eigenvalues[n]`.

        Parameters
        ----------
        energies : array_like
            Values for which the DOS is calculated. Default: min/max from Bands().energy, subdivided in 100 parts [ev].
        broadening : float
            Controls the width of the Gaussian broadening applied to the DOS. Default: 0.05 [ev].
        Returns
        -------
        :class:`~pybinding.Series`
        """
        if energies is None:
            energies = np.linspace(np.nanmin(self.energy), np.nanmax(self.energy), 100)
        if broadening is None:
            broadening = (np.nanmax(self.energy) - np.nanmin(self.energy)) / 100
        scale = 1 / (broadening * np.sqrt(2 * np.pi) * self.energy.shape[0])
        dos = np.zeros(len(energies))
        for eigenvalue in self.energy:
            delta = eigenvalue[:, np.newaxis] - energies
            dos += scale * np.sum(np.exp(-0.5 * delta**2 / broadening**2), axis=0)
        return Series(energies, dos, labels=dict(variable="E (eV)", data="DOS"))


@pickleable
class FatBands(Bands):
    """Band structure with data per k-point, like SOC or pDOS

    Attributes
    ----------
    bands : :class:`Bands`
        The bands on wich the data is written
    data : array_like
        An array of values wich were computed as a function of the bands.k_path.
        It can be 2D or 3D. In the latter case each column represents the result
        of a different function applied to the same `variable` input.
    labels : dict
        Plot labels: 'data', 'title' and 'columns'.
    """
    def __init__(self, bands: Bands, data: ArrayLike, labels: Optional[dict] = None):
        super().__init__(bands.k_path, bands.energy)
        self.data = np.atleast_2d(data)
        self.labels = with_defaults(
            labels, variable="E (eV)", data="pDOS", columns="", title="",
            orbitals=[str(i) for i in range(self.data.shape[1])] if self.data.ndim == 2 else [])

    def with_data(self, data: np.ndarray) -> 'FatBands':
        """Return a copy of this result object with different data"""
        result = copy(self)
        result.data = data
        return result

    def __add__(self, other: 'FatBands') -> 'FatBands':
        """Add together the data of two FatBands object in a new object."""
        if self.data.ndim < other.data.ndim:
            # keep information about the orbitals, so take the other series as a reference
            return other.with_data(self.data[:, :, np.newaxis] + other.data)
        elif self.data.ndim > other.data.ndim:
            return self.with_data(self.data + other.data[:, :, np.newaxis])
        else:
            return self.with_data(self.data + other.data)

    def __sub__(self, other: 'FatBands') -> 'FatBands':
        """Subtract the data of two FatBands object in a new object."""
        if self.data.ndim < other.data.ndim:
            # keep information about the orbitals, so take the other series as a reference
            return other.with_data(self.data[:, :, np.newaxis] - other.data)
        elif self.data.ndim > other.data.ndim:
            return self.with_data(self.data - other.data[:, :, np.newaxis])
        else:
            return self.with_data(self.data - other.data)

    def reduced(self, columns: Optional[List[int]] = None, orbitals: Optional[List[str]] = None,
                fill_other: float = 0.) -> 'FatBands':
        """Return a copy where the data is summed over the columns

        Only applies to results which may have multiple columns of data, e.g.
        results for multiple orbitals for LDOS calculation.

        Parameters
        ----------
        columns : Optional[List[int]]
            The colummns to contract to the new array.
            The length of `columns` agrees with the dimensions of data.shape[2].
            The value at each position corresponds to the new column of the new Series object
        orbitals: Optional[List[str]]
            Optional new list of entries for the `orbitals` label in `labels`
        fill_other : float
            In case an array is made with a new column, fill it with this value. Default: 0.
        """
        data = self.data
        if data.ndim == 2:
            data = self.data[:, np.newaxis]
        col_idx = np.array(columns or np.zeros(data.shape[2]), dtype=int)
        if np.all(col_idx == 0):
            # case where all the axis are summed over, no 'orbital' label is needed
            return self.with_data(data.sum(axis=2))
        col_max = np.max(col_idx) + 1
        if orbitals is None:
            orb_list = [str(i) for i in range(col_max)]
            for c_i in np.unique(col_idx):
                orb_list[c_i] = self.labels["orbitals"][np.argmax(col_idx == c_i)]
        else:
            orb_list = orbitals
        data_out = np.full((data.shape[0], data.shape[1], col_max), fill_other)
        for c_i in np.unique(col_idx):
            data_out[:, :, c_i] = np.nansum(data[:, :, col_idx == c_i], axis=2)
        fatbands_out = self.with_data(data_out)
        fatbands_out.labels["orbitals"] = orb_list
        return fatbands_out

    def plot(self, point_labels: Optional[List[str]] = None, ax: Optional[plt.Axes] = None,
                  **kwargs) -> Optional[List[PathCollection]]:
        """Line plot of the band structure with the given data

        Parameters
        ----------
        point_labels : Optional[List[str]]
            Labels for the `k_points`.
        ax : Optional[plt.Axes]
            The Axis to plot the bands on.
        **kwargs
            Forwarded to `plt.plot()`.
        """
        if ax is None:
            ax = plt.gca()
        k_space = np.ones(self.energy.shape) * self.k_path.as_1d()[:, np.newaxis]
        lines = []
        data_length = self.data.shape[2] if self.data.ndim == 3 else 1
        for d_i in range(data_length):
            lines.append(ax.scatter(
                k_space,
                self.energy,
                s=np.nan_to_num(np.abs(self.data[:, :, d_i]) if self.data.ndim == 3 else self.data) * 20,
                alpha=0.5,
                **kwargs
            ))
        ax.legend(lines, self.labels["orbitals"], title=self.labels["columns"])
        ax.set_title(self.labels["title"])
        self._decorate_plot(point_labels, ax)
        return lines

    def plot_bands(self, **kwargs) -> List[plt.Line2D]:
        """Line plot of the band structure like in Bands."""
        return super().plot(**kwargs)

    def line_plot(self, point_labels: Optional[List[str]] = None, ax: Optional[plt.Axes] = None, idx: int = 0,
                  plot_colorbar: bool = True, **kwargs) -> Optional[LineCollection]:
        """Line plot of the band structure with the color of the lines the data of the FatBands.

        Parameters
        ----------
        point_labels : Optional[List[str]]
            Labels for the `k_points`.
        ax : Optional[plt.Axes]
            The Axis to plot the bands on.
        idx : int
            The i-th column to plot. Default: 0.
        plot_colorbar : bool
            Show also the colorbar.
        **kwargs
            Forwarded to `matplotlib.collection.LineCollection()`.
        """
        if ax is None:
            ax = plt.gca()
        k_space = self.k_path.as_1d()
        data = self.data[:, :, idx] if self.data.ndim == 3 else self.data
        ax.set_xlim(np.nanmin(k_space), np.nanmax(k_space))
        ax.set_ylim(np.nanmin(self.energy), np.nanmax(self.energy))
        ax.set_title(self.labels["title"])
        line = pltutils.plot_color(k_space, self.energy, data[:-1, :], ax, **kwargs)
        self._decorate_plot(point_labels, ax)
        if plot_colorbar:
            plt.colorbar(line, ax=ax, label=self.labels["orbitals"][idx])
        return line

    def dos(self, energies: Optional[ArrayLike] = None, broadening: Optional[float] = None) -> Series:
        r"""Calculate the density of states as a function of energy

        .. math::
            \text{DOS}(E) = \frac{1}{c \sqrt{2\pi}}
                            \sum_n{e^{-\frac{(E_n - E)^2}{2 c^2}}}

        for each :math:`E` in `energies`, where :math:`c` is `broadening` and
        :math:`E_n` is `eigenvalues[n]`.

        Parameters
        ----------
        energies : array_like
            Values for which the DOS is calculated. Default: min/max from Bands().energy, subdivided in 100 parts [ev].
        broadening : float
            Controls the width of the Gaussian broadening applied to the DOS. Default: 0.05 [ev].

        Returns
        -------
        :class:`~pybinding.Series`
        """
        if energies is None:
            energies = np.linspace(np.nanmin(self.energy), np.nanmax(self.energy), 100)
        if broadening is None:
            broadening = (np.nanmax(self.energy) - np.nanmin(self.energy)) / 100
        scale = 1 / (broadening * np.sqrt(2 * np.pi) * self.energy.shape[0])
        data = self.data if self.data.ndim == 3 else self.data[:, :, np.newaxis]
        dos = np.zeros((data.shape[2], len(energies)))
        for i_k, eigenvalue in enumerate(self.energy):
            delta = np.nan_to_num(eigenvalue[:, np.newaxis]) - energies
            gauss = np.exp(-0.5 * delta**2 / broadening**2)
            datal = np.nan_to_num(data[i_k])
            dos += scale * np.sum(datal[:, :, np.newaxis] * gauss[:, np.newaxis, :], axis=0)
        return Series(energies, dos.T, labels=self.labels)


@pickleable
class Sweep:
    """2D parameter sweep with `x` and `y` 1D array parameters and `data` 2D array result

    Attributes
    ----------
    x : array_like
        1D array with x-axis values -- usually the primary parameter being swept.
    y : array_like
        1D array with y-axis values -- usually the secondary parameter.
    data : array_like
        2D array with `shape == (x.size, y.size)` containing the main result data.
    labels : dict
        Plot labels: 'title', 'x', 'y' and 'data'.
    tags : dict
        Any additional user defined variables.
    """
    def __init__(self, x: ArrayLike, y: ArrayLike, data: ArrayLike, labels: Optional[dict] = None,
                 tags: Optional[dict] = None):
        self.x = np.atleast_1d(x)
        self.y = np.atleast_1d(y)
        self.data = np.atleast_2d(data)

        self.labels = with_defaults(labels, title="", x="x", y="y", data="data")
        self.tags = tags

    def __getitem__(self, item: Union[Tuple[int, int], int]) -> 'Sweep':
        """Same rules as numpy indexing"""
        if isinstance(item, tuple):
            idx_x, idx_y = item
        else:
            idx_x = item
            idx_y = slice(None)
        return self._with_data(self.x[idx_x], self.y[idx_y], self.data[idx_x, idx_y])

    def _with_data(self, x: ArrayLike, y: ArrayLike, data: ArrayLike) -> 'Sweep':
        return self.__class__(x, y, data, self.labels, self.tags)

    @property
    def _plain_labels(self) -> dict:
        """Labels with latex symbols stripped out"""
        trans = str.maketrans('', '', '$\\')
        return {k: v.translate(trans) for k, v in self.labels.items()}

    def _xy_grids(self) -> tuple[np.ndarray, np.ndarray]:
        """Expand x and y into 2D arrays matching data."""
        xgrid = np.column_stack([self.x] * self.y.size)
        ygrid = np.row_stack([self.y] * self.x.size)
        return xgrid, ygrid

    def save_txt(self, filename: str) -> None:
        """Save text file with 3 columns: x, y, data.

        Parameters
        ----------
        filename : str
        """
        with open(filename, 'w') as file:
            file.write("#{x:>11} {y:>12} {data:>12}\n".format(**self._plain_labels))

            xgrid, ygrid = self._xy_grids()
            for row in zip(xgrid.flat, ygrid.flat, self.data.flat):
                values = ("{:12.5e}".format(v) for v in row)
                file.write(" ".join(values) + "\n")

    def cropped(self, x: Optional[tuple[float, float]] = None, y: Optional[tuple[float, float]] = None) -> 'Sweep':
        """Return a copy with data cropped to the limits in the x and/or y axes

        A call with x=[-1, 2] will leave data only where -1 <= x <= 2.

        Parameters
        ----------
        x, y : Tuple[float, float]
            Min and max data limit.

        Returns
        -------
        :class:`~pybinding.Sweep`
        """
        idx_x = np.logical_and(x[0] <= self.x, self.x <= x[1]) if x else np.arange(self.x.size)
        idx_y = np.logical_and(y[0] <= self.y, self.y <= y[1]) if y else np.arange(self.y.size)
        return self._with_data(self.x[idx_x], self.y[idx_y], self.data[np.ix_(idx_x, idx_y)])

    def mirrored(self, axis: Literal['x', 'y'] = 'x') -> 'Sweep':
        """Return a copy with data mirrored in around specified axis

         Only makes sense if the axis starts at 0.

        Parameters
        ----------
        axis : 'x' or 'y'

        Returns
        -------
        :class:`~pybinding.Sweep`
        """
        if axis == 'x':
            x = np.concatenate((-self.x[::-1], self.x[1:]))
            data = np.vstack((self.data[::-1], self.data[1:]))
            return self._with_data(x, self.y, data)
        elif axis == 'y':
            y = np.concatenate((-self.y[::-1], self.y[1:]))
            data = np.hstack((self.data[:, ::-1], self.data[:, 1:]))
            return self._with_data(self.x, y, data)
        else:
            RuntimeError("Invalid axis")

    def interpolated(self, mul: Optional[Union[int, tuple[int, int]]] = None,
                     size: Optional[Union[int, tuple[int, int]]] = None,
                     kind: Literal['linear', 'nearest', 'nearest-up', 'zero', 'slinear', 'quadratic', 'cubic',
                                   'previous', 'next', 'zero', 'slinear', 'quadratic', 'cubic'] = 'linear') -> 'Sweep':
        """Return a copy with interpolate data using :class:`scipy.interpolate.interp1d`

        Call with `mul=2` to double the size of the x-axis and interpolate data to match.
        To interpolate in both axes pass a tuple, e.g. `mul=(4, 2)`.

        Parameters
        ----------
        mul : Union[int, Tuple[int, int]]
            Number of times the size of the axes should be multiplied.
        size : Union[int, Tuple[int, int]]
            New size of the axes. Zero will leave size unchanged.
        kind
            Forwarded to :class:`scipy.interpolate.interp1d`.

        Returns
        -------
        :class:`~pybinding.Sweep`
        """
        if not mul and not size:
            return self

        from scipy.interpolate import interp1d
        x, y, data = self.x, self.y, self.data

        if mul:
            try:
                mul_x, mul_y = mul
            except TypeError:
                mul_x, mul_y = mul, 1
            size_x = x.size * mul_x
            size_y = y.size * mul_y
        else:
            try:
                size_x, size_y = size
            except TypeError:
                size_x, size_y = size, 0

        if size_x > 0 and size_x != x.size:
            interpolate = interp1d(x, data, axis=0, kind=kind)
            x = np.linspace(x.min(), x.max(), size_x, dtype=x.dtype)
            data = interpolate(x)

        if size_y > 0 and size_y != y.size:
            interpolate = interp1d(y, data, kind=kind)
            y = np.linspace(y.min(), y.max(), size_y, dtype=y.dtype)
            data = interpolate(y)

        return self._with_data(x, y, data)

    def _convolved(self, sigma: float, axis: Literal['x', 'y'] = 'x') -> 'Sweep':
        """Return a copy where the data is convolved with a Gaussian function

        Parameters
        ----------
        sigma : float
            Gaussian broadening.
        axis : 'x' or 'y'

        Returns
        -------
        :class:`~pybinding.Sweep`
        """
        def convolve(v, data0):
            v0 = v[v.size // 2]
            gaussian = np.exp(-0.5 * ((v - v0) / sigma)**2)
            gaussian /= gaussian.sum()

            extend = 10  # TODO: rethink this
            data1 = np.concatenate((data0[extend::-1], data0, data0[:-extend:-1]))
            data1 = np.convolve(data1, gaussian, 'same')
            return data1[extend:-extend]

        x, y, data = self.x, self.y, self.data.copy()

        if 'x' in axis:
            for i in range(y.size):
                data[:, i] = convolve(x, data[:, i])
        if 'y' in axis:
            for i in range(x.size):
                data[i, :] = convolve(y, data[i, :])

        return self._with_data(x, y, data)

    def plot(self, ax: Optional[plt.Axes] = None, **kwargs) -> matplotlib.collections.QuadMesh:
        """Plot a 2D colormap of :attr:`Sweep.data`

        Parameters
        ----------
        ax : Optional[plt.Axes]
            The axis to plot on.
        **kwargs
            Forwarded to :func:`matplotlib.pyplot.pcolormesh`.
        """
        if ax is None:
            ax = plt.gca()
        mesh = ax.pcolormesh(self.x, self.y, self.data.T,
                             **with_defaults(kwargs, cmap='RdYlBu_r', rasterized=True))
        ax.set_xlim(self.x.min(), self.x.max())
        ax.set_ylim(self.y.min(), self.y.max())

        ax.set_title(self.labels['title'])
        ax.set_xlabel(self.labels['x'])
        ax.set_ylabel(self.labels['y'])

        return mesh

    def colorbar(self, **kwargs):
        """Draw a colorbar with the label of :attr:`Sweep.data`"""
        return pltutils.colorbar(**with_defaults(kwargs, label=self.labels['data']))

    def _plot_slice(self, axis: Literal['x', 'y'], x: np.ndarray, y: ArrayLike, value: float,
                    ax: Optional[plt.Axes] = None, **kwargs) -> None:
        if ax is None:
            ax = plt.gca()
        ax.plot(x, y, **kwargs)

        split = self.labels[axis].split(' ', 1)
        label = split[0]
        unit = '' if len(split) == 1 else split[1].strip('()')
        ax.set_title('{}, {} = {:.2g} {}'.format(self.labels['title'], label, value, unit))

        ax.set_xlim(x.min(), x.max())
        ax.set_xlabel(self.labels['x' if axis == 'y' else 'y'])
        ax.set_ylabel(self.labels['data'])
        pltutils.despine(ax=ax)

    def _slice_x(self, x: float) -> np.ndarray:
        """Return a slice of data nearest to x and the found values of x.

        Parameters
        ----------
        x : float
        """
        idx = np.abs(self.x - x).argmin()
        return self.data[idx, :], self.x[idx]

    def _slice_y(self, y: float) -> np.ndarray:
        """Return a slice of data nearest to y and the found values of y.

        Parameters
        ----------
        y : float
        """
        idx = np.abs(self.y - y).argmin()
        return self.data[:, idx], self.y[idx]

    def plot_slice_x(self, x: ArrayLike, **kwargs) -> None:
        z, value = self._slice_x(x)
        self._plot_slice('x', self.y, z, value, **kwargs)

    def plot_slice_y(self, y: ArrayLike, **kwargs) -> None:
        z, value = self._slice_y(y)
        self._plot_slice('y', self.x, z, value, **kwargs)


@pickleable
class NDSweep:
    """ND parameter sweep

    Attributes
    ----------
    variables : tuple of array_like
        The parameters being swept.
    data : np.ndarray
        Main result array with `shape == [len(v) for v in variables]`.
    labels : dict
        Plot labels: 'title', 'x', 'y' and 'data'.
    tags : dict
        Any additional user defined variables.
    """
    def __init__(self, variables: ArrayLike, data: np.ndarray, labels: Optional[dict] = None,
                 tags: Optional[dict] = None):
        self.variables = variables
        self.data = np.reshape(data, [len(v) for v in variables])

        self.labels = with_defaults(labels, title="", axes=[], data="data")
        # alias the first 3 axes to x, y, z for compatibility with Sweep labels
        for axis, label in zip('xyz', self.labels['axes']):
            self.labels[axis] = label

        self.tags = tags


@pickleable
class Disentangle:
    def __init__(self, overlap_matrix: np.ndarray):
        """
        A Class to store the product matrix for a wavefunction, not the wavefunction itself.
            Main application is for disentangling for the band structure.

        Parameters
        ----------
        overlap_matrix : np.ndarray
            Array of the product of the wave function between two k-points.
        """
        self.overlap_matrix: np.ndarray = overlap_matrix
        self.threshold: float = np.abs(2 * np.shape(overlap_matrix)[1]) ** -0.25
        self._disentangle_matrix: Optional[np.ndarray] = None
        self._routine: int = 1

    @property
    def disentangle_matrix(self) -> Tuple[np.ndarray, np.ndarray]:
        """ Give back the reordering for the band structure.

        Returns : Tuple[np.ndarray(), np.ndarray()]
            2D array with the [to-index, relative changes index] of the band for each step
        """
        if self._disentangle_matrix is None:
            self._disentangle_matrix = self._calc_disentangle_matrix()
        return self._disentangle_matrix

    def __call__(self, matrix: np.ndarray) -> np.ndarray:
        """ Apply the disentanglement on a matrix, wrapper for Disentangle._apply_disentanglement()

        usage :
            energy_sorted = Disentangle(energy_unsorted)

        Parameters : np.ndarray
                The matrix to reorder
        Returns : np.ndarray
            The reordered matrix
        """
        return self._apply_disentanglement(matrix)

    @property
    def routine(self) -> int:
        """ Give back the routine for the reordering.

        Returns : int
            The integer for the routine:
                0 -> The bands are ordered from low to high
                1 -> The scipy.optimize.linear_sum_assignment
        """
        return self._routine

    @routine.setter
    def routine(self, use: int):
        """ Set the routine for the reordering

        Parameters : int
            The integer for the routine:
                0 -> The bands are ordered from low to high
                1 -> The scipy.optimize.linear_sum_assignment
        """
        self._routine = use
        self._disentangle_matrix = None

    def _calc_disentangle_matrix(self) -> Tuple[np.ndarray, np.ndarray]:
        """ Calculate the changes in index for the band structure of which the overlap matrix is given

        Parameters
        Returns : Tuple[np.ndarray(), np.ndarray()]
            2D array with the [to-index, relative changes index] of the band for each step
        """
        assert len(self.overlap_matrix.shape) == 3, \
            "The overlap has the wrong shape, {0} and not 3".format(len(self.overlap_matrix.shape))

        n_k, n_b, n_b_2 = self.overlap_matrix.shape
        assert n_b == n_b_2, "currently, only square matrices can be used, {0} != {1}".format(n_b, n_b_2)
        # there is one more k-point
        n_k += 1

        # matrix to store the changes of the index i
        ind = np.zeros((n_k, n_b), dtype=int)

        # matrix to store value of the overlap
        keep = np.zeros((n_k, n_b), dtype=bool)

        if self.routine == 0:
            func = self._linear_sum_approx
        elif self.routine == 1:
            func = self._linear_sum_scipy
        else:
            assert False, "The value for ise_scipy of {0} doesn't even exist".format(self.routine)

        # loop over all the k-points
        for i_k in range(n_k):
            if i_k == 0:
                ind[i_k], keep[i_k] = np.arange(n_b, dtype=int), np.full(n_b, True, dtype=bool)
            else:
                ind[i_k], keep[i_k] = func(self.overlap_matrix[i_k - 1, ind[i_k - 1], :])

        working_indices = np.zeros((n_k, n_b), dtype=int)
        tmp_w_i = np.arange(n_b, dtype=int)
        for i_k in range(n_k):
            for i_b in range(n_b):
                if not keep[i_k, i_b] and not self.threshold == 0:
                    tmp_w_i[i_b] = np.max(tmp_w_i) + 1
            working_indices[i_k] = tmp_w_i
        if self.threshold == 0:
            assert np.max(working_indices) + 1 == n_b, "This shouldn't happen, the system should not increase in size."
        return ind, working_indices

    def _linear_sum_approx(self, matrix: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """ Function to calculate the equivalent of scipy.optimize.lineair_sum_assignment"""
        assert len(matrix.shape) == 2, "The matrix must have two dimensions"

        n_b, n_b_2 = matrix.shape
        assert n_b == n_b_2, "currently, only square matrices can be used, {0} != {1}".format(n_b, n_b_2)

        # matrix to store the changes of the index i
        ind = np.zeros(n_b, dtype=int)
        # matrix to store value of the overlap
        keep = np.zeros(n_b, dtype=bool)

        index_all = np.arange(n_b, dtype=int).tolist()
        for i_b in range(n_b):
            # find the new index where the value is the largest, considering only the new indices. The result
            # of 'i_max' is the relative index of the 'new indices' that aren't chosen yet
            i_max = np.argmax([matrix[i_b][i] for i in index_all])
            # first convert the relative new index to the new index, and find with old index this corresponds
            i_new = index_all.pop(i_max)
            ind[i_b] = i_new
            keep[i_b] = matrix[i_b, i_new] > self.threshold
        return ind, keep

    def _linear_sum_scipy(self, matrix: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """ Wrapper for scipy.optimize.lineair_sum_assignment"""
        from scipy.optimize import linear_sum_assignment
        orig, perm = linear_sum_assignment(-matrix)
        assert np.all(orig == np.arange(matrix.shape[0])), \
            "The orig should be a list from 1 to number of bands, but is {0}".format(orig)
        return np.array(perm, dtype=int), np.array(matrix[orig, perm] > self.threshold, dtype=bool)

    def _apply_disentanglement(self, matrix: np.ndarray) -> np.ndarray:
        """ Apply the disentanglement on a matrix

        Parameters:
            matrix : np.ndarray
                The matrix to reorder
        Returns : np.ndarray
            The reordered matrix
        """
        assert len(np.shape(matrix)) >= 2, \
            "The wavefunction has the wrong shape, {0} is smaller than 2".format(len(np.shape(matrix)))

        ind, working_indices = self.disentangle_matrix
        assert len(ind.shape) == 2, \
            "The ind matrix has the wrong shape, {0} and not 2".format(len(ind.shape))
        assert len(working_indices.shape) == 2, \
            "The working_indices matrix has the wrong shape, {0} and not 2".format(len(working_indices.shape))

        assert np.shape(matrix)[:2] == ind.shape, \
            "The shapes of the matrices don't agree (energy - ind), {0} != {1}".format(
                np.shape(matrix)[:2], ind.shape)
        assert np.shape(matrix)[:2] == ind.shape, \
            "The shapes of the matrices don't agree (energy - working_indices), {0} != {1}".format(
                np.shape(matrix)[:2], working_indices.shape)
        n_k, n_b = working_indices.shape
        size = np.array(np.shape(matrix))
        size[1] = np.max(working_indices) + 1
        out_values = np.full(size, np.nan)
        for i_k in range(n_k):
            out_values[i_k, working_indices[i_k]] = matrix[i_k, ind[i_k]]
        return out_values


class SpatialLDOS:
    """Holds the results of :meth:`KPM.calc_spatial_ldos`

    It behaves like a product of a :class:`.Series` and a :class:`.StructureMap`.
    """

    def __init__(self, data: np.ndarray, energy: np.ndarray, structure: Structure):
        self.data = data
        self.energy = energy
        self.structure = structure

    def structure_map(self, energy: float) -> StructureMap:
        """Return a :class:`.StructureMap` of the spatial LDOS at the given energy

        Parameters
        ----------
        energy : float
            Produce a structure map for LDOS data closest to this energy value.

        Returns
        -------
        :class:`.StructureMap`
        """
        idx = np.argmin(abs(self.energy - energy))
        return self.structure.with_data(self.data[idx])

    def ldos(self, position: ArrayLike, sublattice: str = "") -> Series:
        """Return the LDOS as a function of energy at a specific position

        Parameters
        ----------
        position : array_like
        sublattice : Optional[str]

        Returns
        -------
        :class:`.Series`
        """
        idx = self.structure.find_nearest(position, sublattice)
        return Series(self.energy, self.data[:, idx],
                      labels=dict(variable="E (eV)", data="LDOS", columns="orbitals"))


class Wavefunction:
    def __init__(self, bands: Bands, wavefunction: np.ndarray, sublattices: Optional[AliasArray] = None,
                 system=None):
        """ Class to store the results of a Wavefunction.

        Parameters:
            bands : bands
                The band structure, with eigenvalues and k_path, of the wavefunction
            wavefunction : np.ndarray()
                ND-array. The first dimension corresponds with the k-point, the second with the band (sorted values),
                the last index with the relative dimension of the wavefunction. The wavefunction is complex,
                and already rescaled to give a norm of 1. The np.dot-function is used to calculate the overlap
                with the hermitian conjugate.
        """
        self.bands: Bands = bands
        self.wavefunction: np.ndarray = wavefunction
        self._overlap_matrix: Optional[np.ndarray] = None
        self._disentangle: Optional[Disentangle] = None
        self._sublattices: Optional[AliasArray] = sublattices
        self._system = system

    @property
    def overlap_matrix(self) -> np.ndarray:
        """ Give back the overlap matrix

        Returns : np.ndarray
            The overlap matrix between the different k-points.
        """
        if self._overlap_matrix is None:
            self._overlap_matrix = self._calc_overlap_matrix()
        return self._overlap_matrix

    def _calc_overlap_matrix(self) -> np.ndarray:
        """ Calculate the overlap of all the wavefunctions with each other

            Parameters
            Returns : np.ndarray()
                3D array with the relative coverlap between the k-point and the previous k-point
            """
        assert len(self.wavefunction.shape) == 3, \
            "The favefunction has the wrong shape, {0} and not 3".format(len(self.wavefunction.shape))
        n_k = self.wavefunction.shape[0]
        assert n_k > 1, "There must be more than one k-point, first dimension is not larger than 1."
        return np.array([np.abs(self.wavefunction[i_k] @ self.wavefunction[i_k + 1].T.conj())
                         for i_k in range(n_k - 1)])

    @property
    def disentangle(self):
        """ Give back a Disentanlement-class, and save the class for further usage.

        Returns : Disentangle
            Class to perform disentanglement
        """
        if self._disentangle is None:
            self._disentangle = Disentangle(self.overlap_matrix)
        return self._disentangle

    @property
    def bands_disentangled(self) -> Bands:
        """ Disentangle the bands from the wavefunction.

        Returns : Bands
            The reordered eigenvalues in a Bands-class."""
        return Bands(self.bands.k_path, self.disentangle(self.bands.energy))

    @property
    def fatbands(self) -> FatBands:
        """ Return FatBands with the pDOS for each sublattice.

        Returns : FatBands
            The (unsorted) bands with the pDOS.
        """
        probablitiy = np.abs(self.wavefunction ** 2)
        labels = {"data": "pDOS", "columns": "orbital"}
        if self._sublattices is not None:
            mapping = self._sublattices.mapping
            keys = mapping.keys()
            data = np.zeros((self.bands.energy.shape[0], self.bands.energy.shape[1], len(keys)))
            for i_k, key in enumerate(keys):
                data[:, :, i_k] = np.sum(probablitiy[:, :, self._sublattices == key], axis=2)
            labels["orbitals"] = [str(key) for key in keys]
        else:
            data = probablitiy
        return FatBands(self.bands, data, labels)

    @property
    def fatbands_disentangled(self) -> FatBands:
        """ Return FatBands with the pDOS for each sublattice.

        Returns : FatBands
            The (sorted) bands with the pDOS.
        """
        fatbands = self.fatbands
        return FatBands(self.bands_disentangled, self.disentangle(fatbands.data), fatbands.labels)

    def spatial_ldos(self, energies: Optional[ArrayLike] = None,
                     broadening: Optional[float] = None) -> Union[Series, SpatialLDOS]:
        r"""Calculate the spatial local density of states at the given energy

        .. math::
            \text{LDOS}(r) = \frac{1}{c \sqrt{2\pi}}
                             \sum_n{|\Psi_n(r)|^2 e^{-\frac{(E_n - E)^2}{2 c^2}}}

        for each position :math:`r` in `system.positions`, where :math:`E` is `energy`,
        :math:`c` is `broadening`, :math:`E_n` is `eigenvalues[n]` and :math:`\Psi_n(r)`
        is `eigenvectors[:, n]`.

        Parameters
        ----------
        energies : Arraylike
            The energy value for which the spatial LDOS is calculated.
        broadening : float
            Controls the width of the Gaussian broadening applied to the DOS.

        Returns
        -------
        :class:`~pybinding.StructureMap`
        """
        if energies is None:
            energies = np.linspace(np.nanmin(self.bands.energy), np.nanmax(self.bands.energy), 100)
        if broadening is None:
            broadening = (np.nanmax(self.bands.energy) - np.nanmin(self.bands.energy)) / 100
        scale = 1 / (broadening * np.sqrt(2 * np.pi) * self.bands.energy.shape[0])
        ldos = np.zeros((self.wavefunction.shape[2], len(energies)))
        for i_k, eigenvalue in enumerate(self.bands.energy):
            delta = np.nan_to_num(eigenvalue)[:, np.newaxis] - energies
            gauss = np.exp(-0.5 * delta**2 / broadening**2)
            psi2 = np.nan_to_num(np.abs(self.wavefunction[i_k].T)**2)
            ldos += scale * np.sum(psi2[:, :, np.newaxis] * gauss[np.newaxis, :, :], axis=1)
        if self._system is not None:
            return SpatialLDOS(ldos.T, energies, self._system)
        else:
            labels = {"variable": "E (eV)", "data": "sLDOS", "columns": "orbitals"}
            if self._sublattices is not None:
                mapping = self._sublattices.mapping
                keys = mapping.keys()
                data = np.zeros((len(energies), len(keys)))
                for i_k, key in enumerate(keys):
                    data[:, i_k] = np.sum(ldos[self._sublattices == key, :], axis=0)
                labels["orbitals"] = [str(key) for key in keys]
                ldos = data
            else:
                labels["orbitals"] = [str(i) for i in range(self.wavefunction.shape[2])]
                ldos = ldos.T
            return Series(energies, ldos, labels=labels)
