"""Simple Preprocessing Functions

Compositions of these functions are found in sc.preprocess.recipes.
"""

import scipy as sp
import warnings
from scipy.sparse import issparse, isspmatrix_csr, csr_matrix
from sklearn.utils import sparsefuncs
import pandas as pd
import numba
from pandas.api.types import is_categorical_dtype
from anndata import AnnData
from .. import settings as sett
from .. import logging as logg
from ..utils import sanitize_anndata

# install dask if available
try:
    import dask.array as da
except ImportError:
    da = None

# install zappy (which wraps numpy), or fall back to plain numpy
try:
    import zappy.base as np
except ImportError:
    import numpy as np

N_PCS = 50  # default number of PCs


def materialize_as_ndarray(a):
    """Convert distributed arrays to ndarrays."""
    if type(a) in (list, tuple):
        if da is not None and any(isinstance(arr, da.Array) for arr in a):
            return da.compute(*a, sync=True)
        elif hasattr(np, 'asarray'): # zappy case
            return tuple(np.asarray(arr) for arr in a)
    else:
        if da is not None and isinstance(a, da.Array):
            return a.compute()
        elif hasattr(np, 'asarray'): # zappy case
            return np.asarray(a)
    return a


def filter_cells(data, min_counts=None, min_genes=None, max_counts=None,
                 max_genes=None, copy=False):
    """Filter cell outliers based on counts and numbers of genes expressed.

    For instance, only keep cells with at least `min_counts` counts or
    `min_genes` genes expressed. This is to filter measurement outliers, i.e.,
    "unreliable" observations.

    Only provide one of the optional parameters `min_counts`, `min_genes`,
    `max_counts`, `max_genes` per call.

    Parameters
    ----------
    data : :class:`~anndata.AnnData`, `np.ndarray`, `sp.spmatrix`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    min_counts : `int`, optional (default: `None`)
        Minimum number of counts required for a cell to pass filtering.
    min_genes : `int`, optional (default: `None`)
        Minimum number of genes expressed required for a cell to pass filtering.
    max_counts : `int`, optional (default: `None`)
        Maximum number of counts required for a cell to pass filtering.
    max_genes : `int`, optional (default: `None`)
        Maximum number of genes expressed required for a cell to pass filtering.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    If `data` is an :class:`~anndata.AnnData`, filters the object and adds\
    either `n_genes` or `n_counts` to `adata.obs`. Otherwise a tuple

    cell_subset : `np.ndarray`
        Boolean index mask that does filtering. `True` means that the cell is
        kept. `False` means the cell is removed.
    number_per_cell : `np.ndarray`
        Either `n_counts` or `n_genes` per cell.

    Examples
    --------
    >>> adata = sc.datasets.krumsiek11()
    >>> adata.n_obs
    640
    >>> adata.var_names
    ['Gata2' 'Gata1' 'Fog1' 'EKLF' 'Fli1' 'SCL' 'Cebpa'
     'Pu.1' 'cJun' 'EgrNab' 'Gfi1']
    >>> # add some true zeros
    >>> adata.X[adata.X < 0.3] = 0
    >>> # simply compute the number of genes per cell
    >>> sc.pp.filter_cells(adata, min_genes=0)
    >>> adata.n_obs
    640
    >>> adata.obs['n_genes'].min()
    1
    >>> # filter manually
    >>> adata_copy = adata[adata.obs['n_genes'] >= 3]
    >>> adata_copy.obs['n_genes'].min()
    >>> adata.n_obs
    554
    >>> adata.obs['n_genes'].min()
    3
    >>> # actually do some filtering
    >>> sc.pp.filter_cells(adata, min_genes=3)
    >>> adata.n_obs
    554
    >>> adata.obs['n_genes'].min()
    3
    """
    if min_genes is not None and min_counts is not None:
        raise ValueError('Either provide min_counts or min_genes, but not both.')
    if min_genes is not None and max_genes is not None:
        raise ValueError('Either provide min_genes or max_genes, but not both.')
    if min_counts is not None and max_counts is not None:
        raise ValueError('Either provide min_counts or max_counts, but not both.')
    if min_genes is None and min_counts is None and max_genes is None and max_counts is None:
        raise ValueError('Provide one of min_counts, min_genes, max_counts or max_genes.')
    if isinstance(data, AnnData):
        adata = data.copy() if copy else data
        cell_subset, number = materialize_as_ndarray(filter_cells(adata.X, min_counts, min_genes, max_counts, max_genes))
        if min_genes is None and max_genes is None: adata.obs['n_counts'] = number
        else: adata.obs['n_genes'] = number
        adata._inplace_subset_obs(cell_subset)
        return adata if copy else None
    X = data  # proceed with processing the data matrix
    min_number = min_counts if min_genes is None else min_genes
    max_number = max_counts if max_genes is None else max_genes
    number_per_cell = np.sum(X if min_genes is None and max_genes is None
                             else X > 0, axis=1)
    if issparse(X): number_per_cell = number_per_cell.A1
    if min_number is not None:
        cell_subset = number_per_cell >= min_number
    if max_number is not None:
        cell_subset = number_per_cell <= max_number

    s = np.sum(~cell_subset)
    if s > 0:
        logg.info('filtered out {} cells that have'.format(s), end=' ')
        if min_genes is not None or min_counts is not None:
            logg.info('less than',
                   str(min_genes) + ' genes expressed'
                   if min_counts is None else str(min_counts) + ' counts', no_indent=True)
        if max_genes is not None or max_counts is not None:
            logg.info('more than ',
                   str(max_genes) + ' genes expressed'
                   if max_counts is None else str(max_counts) + ' counts', no_indent=True)
    return cell_subset, number_per_cell


def filter_genes(data, min_counts=None, min_cells=None, max_counts=None,
                 max_cells=None, copy=False):
    """Filter genes based on number of cells or counts.

    Keep genes that have at least `min_counts` counts or are expressed in at
    least `min_cells` cells or have at most `max_counts` counts or are expressed
    in at most `max_cells` cells.

    Only provide one of the optional parameters `min_counts`, `min_cells`,
    `max_counts`, `max_cells` per call.

    Parameters
    ----------
    data : :class:`~anndata.AnnData`, `np.ndarray`, `sp.spmatrix`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    min_counts : `int`, optional (default: `None`)
        Minimum number of counts required for a gene to pass filtering.
    min_cells : `int`, optional (default: `None`)
        Minimum number of cells expressed required for a gene to pass filtering.
    max_counts : `int`, optional (default: `None`)
        Maximum number of counts required for a gene to pass filtering.
    max_cells : `int`, optional (default: `None`)
        Maximum number of cells expressed required for a gene to pass filtering.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    If `data` is an :class:`~anndata.AnnData`, filters the object and adds\
    either `n_cells` or `n_counts` to `adata.var`. Otherwise a tuple

    gene_subset : `np.ndarray`
        Boolean index mask that does filtering. `True` means that the gene is
        kept. `False` means the gene is removed.
    number_per_gene : `np.ndarray`
        Either `n_counts` or `n_cells` per gene.
    """
    n_given_options = sum(
        option is not None for option in
        [min_cells, min_counts, max_cells, max_counts])
    if n_given_options != 1:
        raise ValueError(
            'Only provide one of the optional parameters `min_counts`,'
            '`min_cells`, `max_counts`, `max_cells` per call.')

    if isinstance(data, AnnData):
        adata = data.copy() if copy else data
        gene_subset, number = materialize_as_ndarray(filter_genes(adata.X, min_cells=min_cells,
                                           min_counts=min_counts, max_cells=max_cells,
                                           max_counts=max_counts))
        if min_cells is None and max_cells is None:
            adata.var['n_counts'] = number
        else:
            adata.var['n_cells'] = number
        adata._inplace_subset_var(gene_subset)
        return adata if copy else None

    X = data  # proceed with processing the data matrix
    min_number = min_counts if min_cells is None else min_cells
    max_number = max_counts if max_cells is None else max_cells
    number_per_gene = np.sum(X if min_cells is None and max_cells is None
                             else X > 0, axis=0)
    if issparse(X):
        number_per_gene = number_per_gene.A1
    if min_number is not None:
        gene_subset = number_per_gene >= min_number
    if max_number is not None:
        gene_subset = number_per_gene <= max_number

    s = np.sum(~gene_subset)
    if s > 0:
        logg.info('filtered out {} genes that are detected'.format(s), end=' ')
        if min_cells is not None or min_counts is not None:
            logg.info('in less than',
                   str(min_cells) + ' cells'
                   if min_counts is None else str(min_counts) + ' counts', no_indent=True)
        if max_cells is not None or max_counts is not None:
            logg.info('in more than ',
                   str(max_cells) + ' cells'
                   if max_counts is None else str(max_counts) + ' counts', no_indent=True)
    return gene_subset, number_per_gene


def log1p(data, copy=False, chunked=False, chunk_size=None):
    """Logarithmize the data matrix.

    Computes `X = log(X + 1)`, where `log` denotes the natural logarithm.

    Parameters
    ----------
    data : :class:`~anndata.AnnData`, `np.ndarray`, `sp.sparse`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    Returns or updates `data`, depending on `copy`.
    """
    if copy:
        data = data.copy()

    def _log1p(X):
        if issparse(X):
            np.log1p(X.data, out=X.data)
        else:
            np.log1p(X, out=X)

        return X

    if isinstance(data, AnnData):
        if chunked:
            for chunk, start, end in data.chunked_X(chunk_size):
                 data.X[start:end] = _log1p(chunk)
        else:
            _log1p(data.X)
    else:
        _log1p(data)

    return data if copy else None


def sqrt(data, copy=False, chunked=False, chunk_size=None):
    """Square root the data matrix.

    Computes `X = sqrt(X)`.

    Parameters
    ----------
    data : :class:`~scanpy.api.AnnData`, `np.ndarray`, `sp.sparse`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    copy : `bool`, optional (default: `False`)
        If an :class:`~scanpy.api.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    Returns or updates `data`, depending on `copy`.
    """
    if isinstance(data, AnnData):
        adata = data.copy() if copy else data
        if chunked:
            for chunk, start, end in adata.chunked_X(chunk_size):
                adata.X[start:end] = sqrt(chunk)
        else:
            adata.X = sqrt(data.X)
        return adata if copy else None
    X = data  # proceed with data matrix
    if not issparse(X):
        return np.sqrt(X)
    else:
        return X.sqrt()


def pca(data, n_comps=None, zero_center=True, svd_solver='auto', random_state=0,
        return_info=False, use_highly_variable=None, dtype='float32', copy=False,
        chunked=False, chunk_size=None):
    """Principal component analysis [Pedregosa11]_.

    Computes PCA coordinates, loadings and variance decomposition. Uses the
    implementation of *scikit-learn* [Pedregosa11]_.

    Parameters
    ----------
    data : :class:`~anndata.AnnData`, `np.ndarray`, `sp.sparse`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    n_comps : `int`, optional (default: 50)
        Number of principal components to compute.
    zero_center : `bool` or `None`, optional (default: `True`)
        If `True`, compute standard PCA from covariance matrix. If `False`, omit
        zero-centering variables (uses *TruncatedSVD* from scikit-learn), which
        allows to handle sparse input efficiently.
    svd_solver : `str`, optional (default: 'auto')
        SVD solver to use. Either 'arpack' for the ARPACK wrapper in SciPy
        (scipy.sparse.linalg.svds), or 'randomized' for the randomized algorithm
        due to Halko (2009). 'auto' chooses automatically depending on the size
        of the problem.
    random_state : `int`, optional (default: 0)
        Change to use different intial states for the optimization.
    return_info : `bool`, optional (default: `False`)
        Only relevant when not passing an :class:`~anndata.AnnData`: see
        "Returns".
    use_highly_variable : `bool`, optional (default: `None`)
        Whether to use highly variable genes only, stored in .var['highly_variable'].
    dtype : `str` (default: 'float32')
        Numpy data type string to which to convert the result.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned. Is ignored otherwise.
    chunked : `bool`, optional (default: `False`)
        If `True`, perform an incremental PCA on segments of `chunk_size`. The
        incremental PCA automatically zero centers and ignores settings of
        `random_seed` and `svd_solver`. If `False`, perform a full PCA.
    chunk_size : `int`, optional (default: `None`)
        Number of observations to include in each chunk. Required if `chunked`
        is `True`.

    Returns
    -------
    If `data` is array-like and `return_info == False`, only returns `X_pca`,\
    otherwise returns or adds to `adata`:
    X_pca : `.obsm`
         PCA representation of data.
    PCs : `.varm`
         The principal components containing the loadings.
    variance_ratio : `.uns['pca']`
         Ratio of explained variance.
    variance : `.uns['pca']`
         Explained variance, equivalent to the eigenvalues of the covariance matrix.
    """
    # chunked calculation is not randomized, anyways
    if svd_solver in {'auto', 'randomized'} and not chunked:
        logg.info(
            'Note that scikit-learn\'s randomized PCA might not be exactly '
            'reproducible across different computational platforms. For exact '
            'reproducibility, choose `svd_solver=\'arpack\'.` This will likely '
            'become the Scanpy default in the future.')

    if n_comps is None: n_comps = N_PCS

    if isinstance(data, AnnData):
        data_is_AnnData = True
        adata = data.copy() if copy else data
    else:
        data_is_AnnData = False
        adata = AnnData(data)

    logg.msg('computing PCA with n_comps =', n_comps, r=True, v=4)

    if adata.n_vars < n_comps:
        n_comps = adata.n_vars - 1
        logg.msg('reducing number of computed PCs to',
               n_comps, 'as dim of data is only', adata.n_vars, v=4)

    if use_highly_variable is True and 'highly_variable' not in adata.var.keys():
        raise ValueError('Did not find adata.var[\'highly_variable\']. '
                         'Either your data already only consists of highly-variable genes '
                         'or consider running `pp.filter_genes_dispersion` first.')
    if use_highly_variable is None:
        use_highly_variable = True if 'highly_variable' in adata.var.keys() else False
    adata_comp = adata[:, adata.var['highly_variable']] if use_highly_variable else adata

    if chunked:
        if not zero_center or random_state or svd_solver != 'auto':
            logg.msg('Ignoring zero_center, random_state, svd_solver', v=4)

        from sklearn.decomposition import IncrementalPCA

        X_pca = np.zeros((adata_comp.X.shape[0], n_comps), adata_comp.X.dtype)

        pca_ = IncrementalPCA(n_components=n_comps)

        for chunk, _, _ in adata_comp.chunked_X(chunk_size):
            chunk = chunk.toarray() if issparse(chunk) else chunk
            pca_.partial_fit(chunk)

        for chunk, start, end in adata_comp.chunked_X(chunk_size):
            chunk = chunk.toarray() if issparse(chunk) else chunk
            X_pca[start:end] = pca_.transform(chunk)
    else:
        zero_center = zero_center if zero_center is not None else False if issparse(adata_comp.X) else True
        if zero_center:
            from sklearn.decomposition import PCA
            if issparse(adata_comp.X):
                logg.msg('    as `zero_center=True`, '
                       'sparse input is densified and may '
                       'lead to huge memory consumption', v=4)
                X = adata_comp.X.toarray()  # Copying the whole adata_comp.X here, could cause memory problems
            else:
                X = adata_comp.X
            pca_ = PCA(n_components=n_comps, svd_solver=svd_solver, random_state=random_state)
        else:
            from sklearn.decomposition import TruncatedSVD
            logg.msg('    without zero-centering: \n'
                   '    the explained variance does not correspond to the exact statistical defintion\n'
                   '    the first component, e.g., might be heavily influenced by different means\n'
                   '    the following components often resemble the exact PCA very closely', v=4)
            pca_ = TruncatedSVD(n_components=n_comps, random_state=random_state)
            X = adata_comp.X
        X_pca = pca_.fit_transform(X)

    if X_pca.dtype.descr != np.dtype(dtype).descr: X_pca = X_pca.astype(dtype)

    if data_is_AnnData:
        adata.obsm['X_pca'] = X_pca
        if use_highly_variable:
            adata.varm['PCs'] = np.zeros(shape=(adata.n_vars, n_comps))
            adata.varm['PCs'][adata.var['highly_variable']] = pca_.components_.T
        else:
            adata.varm['PCs'] = pca_.components_.T
        adata.uns['pca'] = {}
        adata.uns['pca']['variance'] = pca_.explained_variance_
        adata.uns['pca']['variance_ratio'] = pca_.explained_variance_ratio_
        logg.msg('    finished', t=True, end=' ', v=4)
        logg.msg('and added\n'
                 '    \'X_pca\', the PCA coordinates (adata.obs)\n'
                 '    \'PC1\', \'PC2\', ..., the loadings (adata.var)\n'
                 '    \'pca_variance\', the variance / eigenvalues (adata.uns)\n'
                 '    \'pca_variance_ratio\', the variance ratio (adata.uns)', v=4)
        return adata if copy else None
    else:
        if return_info:
            return X_pca, pca_.components_, pca_.explained_variance_ratio_, pca_.explained_variance_
        else:
            return X_pca


def normalize_per_cell(data, counts_per_cell_after=None, counts_per_cell=None,
                       key_n_counts=None, copy=False, layers=[], use_rep=None,
                       min_counts=1):
    """Normalize total counts per cell.

    Normalize each cell by total counts over all genes, so that every cell has
    the same total count after normalization.

    Similar functions are used, for example, by Seurat [Satija15]_, Cell Ranger
    [Zheng17]_ or SPRING [Weinreb17]_.

    Parameters
    ----------
    data : :class:`~anndata.AnnData`, `np.ndarray`, `sp.sparse`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    counts_per_cell_after : `float` or `None`, optional (default: `None`)
        If `None`, after normalization, each cell has a total count equal
        to the median of the *counts_per_cell* before normalization.
    counts_per_cell : `np.array`, optional (default: `None`)
        Precomputed counts per cell.
    key_n_counts : `str`, optional (default: `'n_counts'`)
        Name of the field in `adata.obs` where the total counts per cell are
        stored.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.
    min_counts : `int`, optional (default: 1)
        Cells with counts less than `min_counts` are filtered out during
        normalization.

    Returns
    -------
    Returns or updates `adata` with normalized version of the original
    `adata.X`, depending on `copy`.

    Examples
    --------
    >>> adata = AnnData(
    >>>     data=np.array([[1, 0], [3, 0], [5, 6]]))
    >>> print(adata.X.sum(axis=1))
    [  1.   3.  11.]
    >>> sc.pp.normalize_per_cell(adata)
    >>> print(adata.obs)
    >>> print(adata.X.sum(axis=1))
       n_counts
    0       1.0
    1       3.0
    2      11.0
    [ 3.  3.  3.]
    >>> sc.pp.normalize_per_cell(adata, counts_per_cell_after=1,
    >>>                          key_n_counts='n_counts2')
    >>> print(adata.obs)
    >>> print(adata.X.sum(axis=1))
       n_counts  n_counts2
    0       1.0        3.0
    1       3.0        3.0
    2      11.0        3.0
    [ 1.  1.  1.]
    """
    if key_n_counts is None: key_n_counts = 'n_counts'
    if isinstance(data, AnnData):
        logg.msg('normalizing by total count per cell', r=True)
        adata = data.copy() if copy else data
        cell_subset, counts_per_cell = materialize_as_ndarray(
                    filter_cells(adata.X, min_counts=min_counts))
        adata.obs[key_n_counts] = counts_per_cell
        adata._inplace_subset_obs(cell_subset)
        normalize_per_cell(adata.X, counts_per_cell_after,
                           counts_per_cell=counts_per_cell[cell_subset])

        layers = adata.layers.keys() if layers == 'all' else layers
        if use_rep == 'after':
            after = counts_per_cell_after
        elif use_rep == 'X':
            after = np.median(counts_per_cell[cell_subset])
        elif use_rep is None:
            after = None
        else: raise ValueError('use_rep should be "after", "X" or None')
        for layer in layers:
            subset, counts = filter_cells(adata.layers[layer],
                    min_counts=min_counts)
            temp = normalize_per_cell(adata.layers[layer], after, counts, copy=True)
            adata.layers[layer] = temp

        logg.msg('    finished', t=True, end=': ')
        logg.msg('normalized adata.X and added', no_indent=True)
        logg.msg('    \'{}\', counts per cell before normalization (adata.obs)'
            .format(key_n_counts))
        return adata if copy else None
    # proceed with data matrix
    X = data.copy() if copy else data
    if counts_per_cell is None:
        if copy == False:
            raise ValueError('Can only be run with copy=True')
        cell_subset, counts_per_cell = filter_cells(X, min_counts=min_counts)
        X = X[cell_subset]
        counts_per_cell = counts_per_cell[cell_subset]
    if counts_per_cell_after is None:
        counts_per_cell_after = np.median(counts_per_cell)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        counts_per_cell += counts_per_cell == 0
        counts_per_cell /= counts_per_cell_after
        if not issparse(X): X /= materialize_as_ndarray(counts_per_cell[:, np.newaxis])
        else: sparsefuncs.inplace_row_scale(X, 1/counts_per_cell)
    return X if copy else None


def normalize_per_cell_weinreb16_deprecated(X, max_fraction=1,
                                            mult_with_mean=False):
    """Normalize each cell [Weinreb17]_.

    This is a deprecated version. See `normalize_per_cell` instead.

    Normalize each cell by UMI count, so that every cell has the same total
    count.

    Parameters
    ----------
    X : np.ndarray
        Expression matrix. Rows correspond to cells and columns to genes.
    max_fraction : float, optional
        Only use genes that make up more than max_fraction of the total
        reads in every cell.
    mult_with_mean: bool, optional
        Multiply the result with the mean of total counts.

    Returns
    -------
    X_norm : np.ndarray
        Normalized version of the original expression matrix.
    """
    if max_fraction < 0 or max_fraction > 1:
        raise ValueError('Choose max_fraction between 0 and 1.')
        
    counts_per_cell = X.sum(1).A1 if issparse(X) else X.sum(1)
    gene_subset = np.all(X <= counts_per_cell[:, None] * max_fraction, axis=0)
    if issparse(X): gene_subset = gene_subset.A1
    tc_include = X[:, gene_subset].sum(1).A1 if issparse(X) else X[:, gene_subset].sum(1)

    X_norm = X.multiply(csr_matrix(1/tc_include[:, None])) if issparse(X) else X / tc_include[:, None]
    if mult_with_mean:
        X_norm *= np.mean(counts_per_cell)

    return X_norm


def regress_out(adata, keys, n_jobs=None, copy=False):
    """Regress out unwanted sources of variation.

    Uses simple linear regression. This is inspired by Seurat's `regressOut`
    function in R [Satija15].

    Parameters
    ----------
    adata : :class:`~anndata.AnnData`
        The annotated data matrix.
    keys : `str` or list of `str`
        Keys for observation annotation on which to regress on.
    n_jobs : `int` or `None`, optional. If None is given, then the n_jobs seting is used (default: `None`)
        Number of jobs for parallel computation.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    Depending on `copy` returns or updates `adata` with the corrected data matrix.
    """
    logg.info('regressing out', keys, r=True)
    if issparse(adata.X):
        logg.info('    sparse input is densified and may '
                  'lead to high memory use')
    adata = adata.copy() if copy else adata
    if isinstance(keys, str):
        keys = [keys]

    if issparse(adata.X):
        adata.X = adata.X.toarray()

    n_jobs = sett.n_jobs if n_jobs is None else n_jobs

    # regress on a single categorical variable
    sanitize_anndata(adata)
    variable_is_categorical = False
    if keys[0] in adata.obs_keys() and is_categorical_dtype(adata.obs[keys[0]]):
        if len(keys) > 1:
            raise ValueError(
                'If providing categorical variable, '
                'only a single one is allowed. For this one '
                'we regress on the mean for each category.')
        logg.msg('... regressing on per-gene means within categories')
        regressors = np.zeros(adata.X.shape, dtype='float32')
        for category in adata.obs[keys[0]].cat.categories:
            mask = (category == adata.obs[keys[0]]).values
            for ix, x in enumerate(adata.X.T):
                regressors[mask, ix] = x[mask].mean()
        variable_is_categorical = True
    # regress on one or several ordinal variables
    else:
        # create data frame with selected keys (if given)
        if keys:
            regressors = adata.obs[keys]
        else:
            regressors = adata.obs.copy()

        # add column of ones at index 0 (first column)
        regressors.insert(0, 'ones', 1.0)

    len_chunk = np.ceil(min(1000, adata.X.shape[1]) / n_jobs).astype(int)
    n_chunks = np.ceil(adata.X.shape[1] / len_chunk).astype(int)

    tasks = []
    # split the adata.X matrix by columns in chunks of size n_chunk (the last chunk could be of smaller
    # size than the others)
    chunk_list = np.array_split(adata.X, n_chunks, axis=1)
    if variable_is_categorical:
        regressors_chunk = np.array_split(regressors, n_chunks, axis=1)
    for idx, data_chunk in enumerate(chunk_list):
        # each task is a tuple of a data_chunk eg. (adata.X[:,0:100]) and
        # the regressors. This data will be passed to each of the jobs.
        if variable_is_categorical:
            regres = regressors_chunk[idx]
        else:
            regres = regressors
        tasks.append(tuple((data_chunk, regres, variable_is_categorical)))

    if n_jobs > 1 and n_chunks > 1:
        import multiprocessing
        pool = multiprocessing.Pool(n_jobs)
        res = pool.map_async(_regress_out_chunk, tasks).get(9999999)
        pool.close()

    else:
        res = list(map(_regress_out_chunk, tasks))

    # res is a list of vectors (each corresponding to a regressed gene column).
    # The transpose is needed to get the matrix in the shape needed
    adata.X = np.vstack(res).T.astype(adata.X.dtype)
    logg.info('    finished', t=True)
    return adata if copy else None


def _regress_out_chunk(data):
    # data is a tuple containing the selected columns from adata.X
    # and the regressors dataFrame
    data_chunk = data[0]
    regressors = data[1]
    variable_is_categorical = data[2]

    responses_chunk_list = []
    import statsmodels.api as sm
    from statsmodels.tools.sm_exceptions import PerfectSeparationError

    for col_index in range(data_chunk.shape[1]):
        if variable_is_categorical:
            regres = np.c_[np.ones(regressors.shape[0]), regressors[:, col_index]]
        else:
            regres = regressors
        try:
            result = sm.GLM(data_chunk[:, col_index], regres, family=sm.families.Gaussian()).fit()
            new_column = result.resid_response
        except PerfectSeparationError:  # this emulates R's behavior
            logg.warn('Encountered PerfectSeparationError, setting to 0 as in R.')
            new_column = np.zeros(data_chunk.shape[0])

        responses_chunk_list.append(new_column)

    return np.vstack(responses_chunk_list)


def scale(data, zero_center=True, max_value=None, copy=False):
    """Scale data to unit variance and zero mean.

    Parameters
    ----------
    data : :class:`~anndata.AnnData`, `np.ndarray`, `sp.sparse`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    zero_center : `bool`, optional (default: `True`)
        If `False`, omit zero-centering variables, which allows to handle sparse
        input efficiently.
    max_value : `float` or `None`, optional (default: `None`)
        Clip (truncate) to this value after scaling. If `None`, do not clip.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    Depending on `copy` returns or updates `adata` with a scaled `adata.X`.
    """
    if isinstance(data, AnnData):
        adata = data.copy() if copy else data
        # need to add the following here to make inplace logic work
        if zero_center and issparse(adata.X):
            logg.msg(
                '... scale_data: as `zero_center=True`, sparse input is '
                'densified and may lead to large memory consumption')
            adata.X = adata.X.toarray()
        scale(adata.X, zero_center=zero_center, max_value=max_value, copy=False)
        return adata if copy else None
    X = data.copy() if copy else data  # proceed with the data matrix
    zero_center = zero_center if zero_center is not None else False if issparse(X) else True
    if not zero_center and max_value is not None:
        logg.msg(
            '... scale_data: be careful when using `max_value` without `zero_center`',
            v=4)
    if max_value is not None:
        logg.msg('... clipping at max_value', max_value)
    if zero_center and issparse(X):
        logg.msg('... scale_data: as `zero_center=True`, sparse input is '
                 'densified and may lead to large memory consumption, returning copy',
                 v=4)
        X = X.toarray()
        copy = True
    _scale(X, zero_center)
    if max_value is not None: X[X > max_value] = max_value
    return X if copy else None


def subsample(data, fraction=None, n_obs=None, random_state=0, copy=False):
    """Subsample to a fraction of the number of observations.

    Parameters
    ----------
    data : :class:`~anndata.AnnData`, `np.ndarray`, `sp.sparse`
        The (annotated) data matrix of shape `n_obs` × `n_vars`. Rows correspond
        to cells and columns to genes.
    fraction : `float` in [0, 1] or `None`, optional (default: `None`)
        Subsample to this `fraction` of the number of observations.
    n_obs : `int` or `None`, optional (default: `None`)
        Subsample to this number of observations.
    random_state : `int` or `None`, optional (default: 0)
        Random seed to change subsampling.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    Returns `X[obs_indices], obs_indices` if data is array-like, otherwise
    subsamples the passed :class:`~anndata.AnnData` (`copy == False`) or
    returns a subsampled copy of it (`copy == True`).
    """
    np.random.seed(random_state)
    old_n_obs = data.n_obs if isinstance(data, AnnData) else data.shape[0]
    if n_obs is not None:
        new_n_obs = n_obs
    elif fraction is not None:
        if fraction > 1 or fraction < 0:
            raise ValueError('`fraction` needs to be within [0, 1], not {}'
                             .format(fraction))
        new_n_obs = int(fraction * old_n_obs)
        logg.msg('... subsampled to {} data points'.format(new_n_obs))
    else:
        raise ValueError('Either pass `n_obs` or `fraction`.')
    obs_indices = np.random.choice(old_n_obs, size=new_n_obs, replace=False)
    if isinstance(data, AnnData):
        adata = data.copy() if copy else data
        adata._inplace_subset_obs(obs_indices)
        return adata if copy else None
    else:
        X = data
        return X[obs_indices], obs_indices


def downsample_counts(adata, target_counts=20000, random_state=0, 
                      replace=True, copy=False):
    """Downsample counts so that each cell has no more than `target_counts`.

    Cells with fewer counts than `target_counts` are unaffected by this. This
    has been implemented by M. D. Luecken.

    Parameters
    ----------
    adata : :class:`~anndata.AnnData`
        Annotated data matrix.
    target_counts : `int` (default: 20,000)
        Target number of counts for downsampling. Cells with more counts than
        'target_counts' will be downsampled to have 'target_counts' counts.
    random_state : `int` or `None`, optional (default: 0)
        Random seed to change subsampling.
    replace : `bool`, optional (default: `True`)
        Whether to sample the counts with replacement.
    copy : `bool`, optional (default: `False`)
        If an :class:`~anndata.AnnData` is passed, determines whether a copy
        is returned.

    Returns
    -------
    Depending on `copy` returns or updates an `adata` with downsampled `.X`.
    """
    if copy:
        adata = adata.copy()
    adata.X = adata.X.astype(np.integer)  # Numba doesn't want floats
    if issparse(adata.X):
        X = adata.X
        if not isspmatrix_csr(X):
            X = csr_matrix(X)
        totals = np.ravel(X.sum(axis=1))
        under_target = np.nonzero(totals > target_counts)[0]
        cols = np.split(X.data.view(), X.indptr[1:-1])
        for colidx in under_target:
            col = cols[colidx]
            downsample_cell(col, target_counts, random_state=random_state,
                            replace=replace, inplace=True)
        if not isspmatrix_csr(adata.X):  # Put it back
            adata.X = type(adata.X)(X)
    else:
        totals = np.ravel(adata.X.sum(axis=1))
        under_target = np.nonzero(totals > target_counts)[0]
        adata.X[under_target, :] = \
            np.apply_along_axis(downsample_cell, 1, adata.X[under_target, :],
                                target_counts, random_state=random_state, replace=replace)
    if copy: return adata

@numba.njit
def downsample_cell(col: np.array, target: int, random_state: int=0, 
                    replace: bool=True, inplace: bool=False):
    """
    Evenly reduce counts in cell to target amount.
    
    This is an internal function and has some restrictions:
    
    * `dtype` of col must be an integer (i.e. satisfy issubclass(col.dtype.type, np.integer))
    * total counts in cell must be less than target
    """
    np.random.seed(random_state)
    cumcounts = col.cumsum()
    if inplace:
        col[:] = 0
    else:
        col = np.zeros_like(col)
    total = cumcounts[-1]
    sample = np.random.choice(total, target, replace=replace)
    sample.sort()
    geneptr = 0
    for count in sample:
        while count >= cumcounts[geneptr]:
            geneptr += 1
        col[geneptr] += 1
    return col


def zscore_deprecated(X):
    """Z-score standardize each variable/gene in X.

    Use `scale` instead.

    Reference: Weinreb et al. (2017).

    Parameters
    ----------
    X : np.ndarray
        Data matrix. Rows correspond to cells and columns to genes.

    Returns
    -------
    XZ : np.ndarray
        Z-score standardized version of the data matrix.
    """
    means = np.tile(np.mean(X, axis=0)[None, :], (X.shape[0], 1))
    stds = np.tile(np.std(X, axis=0)[None, :], (X.shape[0], 1))
    return (X - means) / (stds + .0001)


# --------------------------------------------------------------------------------
# Helper Functions
# --------------------------------------------------------------------------------


def _pca_fallback(data, n_comps=2):
    # mean center the data
    data -= data.mean(axis=0)
    # calculate the covariance matrix
    C = np.cov(data, rowvar=False)
    # calculate eigenvectors & eigenvalues of the covariance matrix
    # use 'eigh' rather than 'eig' since C is symmetric,
    # the performance gain is substantial
    # evals, evecs = np.linalg.eigh(C)
    evals, evecs = sp.sparse.linalg.eigsh(C, k=n_comps)
    # sort eigenvalues in decreasing order
    idcs = np.argsort(evals)[::-1]
    evecs = evecs[:, idcs]
    evals = evals[idcs]
    # select the first n eigenvectors (n is desired dimension
    # of rescaled data array, or n_comps)
    evecs = evecs[:, :n_comps]
    # project data points on eigenvectors
    return np.dot(evecs.T, data.T).T


def _get_mean_var(X):
    # - using sklearn.StandardScaler throws an error related to
    #   int to long trafo for very large matrices
    # - using X.multiply is slower
    if True:
        mean = X.mean(axis=0)
        if issparse(X):
            mean_sq = X.multiply(X).mean(axis=0)
            mean = mean.A1
            mean_sq = mean_sq.A1
        else:
            mean_sq = np.multiply(X, X).mean(axis=0)
        # enforece R convention (unbiased estimator) for variance
        var = (mean_sq - mean**2) * (X.shape[0]/(X.shape[0]-1))
    else:
        from sklearn.preprocessing import StandardScaler
        scaler = StandardScaler(with_mean=False).partial_fit(X)
        mean = scaler.mean_
        # enforce R convention (unbiased estimator)
        var = scaler.var_ * (X.shape[0]/(X.shape[0]-1))
    return mean, var


def _scale(X, zero_center=True):
    # - using sklearn.StandardScaler throws an error related to
    #   int to long trafo for very large matrices
    # - using X.multiply is slower
    #   the result differs very slightly, why?
    if True:
        mean, var = _get_mean_var(X)
        scale = np.sqrt(var)
        if issparse(X):
            if zero_center: raise ValueError('Cannot zero-center sparse matrix.')
            sparsefuncs.inplace_column_scale(X, 1/scale)
        else:
            X -= mean
            X /= scale
    else:
        from sklearn.preprocessing import StandardScaler
        scaler = StandardScaler(with_mean=zero_center, copy=False).partial_fit(X)
        # user R convention (unbiased estimator)
        scaler.scale_ *= np.sqrt(X.shape[0]/(X.shape[0]-1))
        scaler.transform(X)
