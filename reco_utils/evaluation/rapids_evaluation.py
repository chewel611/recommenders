import numpy as np
import cudf as cu

from reco_utils.common.constants import (
    DEFAULT_USER_COL,
    DEFAULT_ITEM_COL,
    DEFAULT_RATING_COL,
    DEFAULT_PREDICTION_COL,
    DEFAULT_K,
    DEFAULT_THRESHOLD,
)


# Memory cleanup just in case

def merge_rating_true_pred(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_rating=DEFAULT_RATING_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
):
    """Join truth and prediction data frames on userID and itemID and return the true
    and predicted rated with the correct index.
    
    Args:
        rating_true (cu.DataFrame): True data
        rating_pred (cu.DataFrame): Predicted data
        col_user (str): column name for user
        col_item (str): column name for item
        col_rating (str): column name for rating
        col_prediction (str): column name for prediction

    Returns:
        np.array: Array with the true ratings
        np.array: Array with the predicted ratings

    """
    suffixes = ["_true", "_pred"]
    rating_true_pred = rating_true.merge(
        rating_pred, on=[col_user, col_item], suffixes=suffixes
    )
    if col_rating in rating_pred.columns:
        col_rating = col_rating + suffixes[0]
    if col_prediction in rating_true.columns:
        col_prediction = col_prediction + suffixes[1]
    return rating_true_pred[col_rating], rating_true_pred[col_prediction]


def rmse(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_rating=DEFAULT_RATING_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
):
    y_true, y_pred = merge_rating_true_pred(
        rating_true=rating_true,
        rating_pred=rating_pred,
        col_user=col_user,
        col_item=col_item,
        col_rating=col_rating,
        col_prediction=col_prediction,
    )
    squared_error = (y_true - y_pred)**2
    return np.sqrt(squared_error.mean())


def mae(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_rating=DEFAULT_RATING_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
):
    y_true, y_pred = merge_rating_true_pred(
        rating_true=rating_true,
        rating_pred=rating_pred,
        col_user=col_user,
        col_item=col_item,
        col_rating=col_rating,
        col_prediction=col_prediction,
    )
    
    return (y_true - y_pred).abs().mean()


def get_top_k_items(
    dataframe, col_user=DEFAULT_USER_COL, col_rating=DEFAULT_RATING_COL, k=DEFAULT_K
):
    """Get the input customer-item-rating tuple in the format of cuDF
    DataFrame, output a cuDF DataFrame in the dense format of top k items
    for each user.
    Note:
        if it is implicit rating, just append a column of constants to be
        ratings.

    Args:
        dataframe (cu.DataFrame): DataFrame of rating data (in the format
        customerID-itemID-rating)
        col_user (str): column name for user
        col_rating (str): column name for rating
        k (int): number of items for each user

    Returns:
        cu.DataFrame: DataFrame of top k items for each user
    """
    # Sort by [user, rating] to replicate groupby user then sort
    group_by_sorted = dataframe.sort_values([col_user, col_rating], ascending=False)
    # Replicate pandas rank() computation
    rating_counts = dataframe.groupby(col_user).count()[col_rating].sort_index(ascending=False)
    group_by_sorted['rank'] = np.concatenate([np.arange(1, cnt + 1) for cnt in rating_counts.to_array()])

    return group_by_sorted.query('rank <= @k').reset_index(drop=True)


def merge_ranking_true_pred(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
    relevancy_method='top_k',
    k=DEFAULT_K,
    threshold=DEFAULT_THRESHOLD,
):
    """Filter truth and prediction data frames on common users

    Args:
        rating_true (cu.DataFrame): True DataFrame
        rating_pred (cu.DataFrame): Predicted DataFrame
        col_user (str): column name for user
        col_item (str): column name for item
        col_prediction (str): column name for prediction
        relevancy_method (str): method for determining relevancy ['top_k', 'by_threshold']
        k (int): number of top k items per user (optional)
        threshold (float): threshold of top items per user (optional)

    Returns:
        cu.DataFrame, cu.DataFrame, int:
            DataFrame of recommendation hits
            DataFrmae of hit counts vs actual relevant items per user
            number of unique user ids
    """
    true_users = cu.DataFrame([(col_user, rating_true[col_user].unique())])
    pred_users = cu.DataFrame([(col_user, rating_pred[col_user].unique())])

    # Make sure the prediction and true data frames have the same set of users
    common_users = true_users.merge(pred_users, on=col_user)
    rating_true_common = rating_true.merge(common_users, on=col_user)
    # RAPIDS computes merge operation in parallel, so the order of result rows are nondeterministic.
    # To make it deterministic, we keep the original index and sort by the index after the merge.
    # We only do care about the order of prediction data because we select top-k predictions later.
    rating_pred_common = rating_pred.reset_index().merge(common_users, on=col_user).set_index('index').sort_index()
    n_users = len(common_users)

    # Return hit items in prediction data frame with ranking information. This is used for calculating NDCG and MAP.
    # Use first to generate unique ranking values for each item. This is to align with the implementation in
    # Spark evaluation metrics, where index of each recommended items (the indices are unique to items) is used
    # to calculate penalized precision of the ordered items.
    if relevancy_method == "top_k":
        top_k = k
    elif relevancy_method == "by_threshold":
        top_k = threshold
    else:
        raise NotImplementedError("Invalid relevancy_method")
    df_top_k = get_top_k_items(
        dataframe=rating_pred_common,
        col_user=col_user,
        col_rating=col_prediction,
        k=top_k,
    )
    df_hit = df_top_k.merge(rating_true_common, on=[col_user, col_item])[
        [col_user, col_item, "rank"]
    ]

    # count the number of hits vs actual relevant items per user
    hit_count = df_hit.groupby(col_user, as_index=False).agg({col_user: "count"}).rename({col_user: "hit"})
    actual_count = rating_true_common.groupby(col_user, as_index=False).agg({col_user: "count"}).rename({col_user: "actual"})
    df_hit_count = hit_count.merge(actual_count, left_index=True, right_index=True).reset_index()
    return df_hit, df_hit_count, n_users


def precision_at_k(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_rating=DEFAULT_RATING_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
    relevancy_method="top_k",
    k=DEFAULT_K,
    threshold=DEFAULT_THRESHOLD,
):
    """Precision at K.

    Args:
        rating_true (cu.DataFrame): True DataFrame
        rating_pred (cu.DataFrame): Predicted DataFrame
        col_user (str): column name for user
        col_item (str): column name for item
        col_rating (str): column name for rating
        col_prediction (str): column name for prediction
        relevancy_method (str): method for determining relevancy ['top_k', 'by_threshold']
        k (int): number of top k items per user
        threshold (float): threshold of top items per user (optional)

    Returns:
        float: precision at k (min=0, max=1)
    """

    df_hit, df_hit_count, n_users = merge_ranking_true_pred(
        rating_true=rating_true,
        rating_pred=rating_pred,
        col_user=col_user,
        col_item=col_item,
        col_prediction=col_prediction,
        relevancy_method=relevancy_method,
        k=k,
        threshold=threshold,
    )

    if df_hit.shape[0] == 0:
        return 0.0

    return (df_hit_count["hit"] / k).sum() / n_users


def recall_at_k(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_rating=DEFAULT_RATING_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
    relevancy_method="top_k",
    k=DEFAULT_K,
    threshold=DEFAULT_THRESHOLD,
):
    """Recall at K.

    Args:
        rating_true (cu.DataFrame): True DataFrame
        rating_pred (cu.DataFrame): Predicted DataFrame
        col_user (str): column name for user
        col_item (str): column name for item
        col_rating (str): column name for rating
        col_prediction (str): column name for prediction
        relevancy_method (str): method for determining relevancy ['top_k', 'by_threshold']
        k (int): number of top k items per user
        threshold (float): threshold of top items per user (optional)

    Returns:
        float: recall at k (min=0, max=1). The maximum value is 1 even when fewer than 
            k items exist for a user in rating_true.
    """

    df_hit, df_hit_count, n_users = merge_ranking_true_pred(
        rating_true=rating_true,
        rating_pred=rating_pred,
        col_user=col_user,
        col_item=col_item,
        col_prediction=col_prediction,
        relevancy_method=relevancy_method,
        k=k,
        threshold=threshold,
    )

    if df_hit.shape[0] == 0:
        return 0.0

    return (df_hit_count["hit"] / df_hit_count["actual"]).sum() / n_users


def ndcg_at_k(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_rating=DEFAULT_RATING_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
    relevancy_method="top_k",
    k=DEFAULT_K,
    threshold=DEFAULT_THRESHOLD,
):
    """Normalized Discounted Cumulative Gain (nDCG).

    Args:
        rating_true (cu.DataFrame): True DataFrame
        rating_pred (cu.DataFrame): Predicted DataFrame
        col_user (str): column name for user
        col_item (str): column name for item
        col_rating (str): column name for rating
        col_prediction (str): column name for prediction
        relevancy_method (str): method for determining relevancy ['top_k', 'by_threshold']
        k (int): number of top k items per user
        threshold (float): threshold of top items per user (optional)

    Returns:
        float: nDCG at k (min=0, max=1).
    """

    df_hit, df_hit_count, n_users = merge_ranking_true_pred(
        rating_true=rating_true,
        rating_pred=rating_pred,
        col_user=col_user,
        col_item=col_item,
        col_prediction=col_prediction,
        relevancy_method=relevancy_method,
        k=k,
        threshold=threshold,
    )

    if df_hit.shape[0] == 0:
        return 0.0

    def idcg(actual, idcg, k, log1p):
        for i, v in enumerate(actual):
            # Calculate sum(1 / np.log1p(range(1, min(v, k) + 1)))
            idcg[i] = 0
            for j in range(0, min(v, k)):
                idcg[i] += 1 / log1p[j] 

    def dcg(rank, dcg, log1p):
        for i, v in enumerate(rank):
            dcg[i] = 1 / log1p[v-1]

    log1p = np.log1p(np.arange(1, k+1))
            
    # calculate discounted gain for hit items
    df_dcg = df_hit.copy()
    # relevance in this case is always 1
    df_dcg = df_dcg.apply_rows(
        dcg,
        incols=['rank'],
        outcols=dict(dcg=np.float64),
        kwargs=dict(log1p=log1p)
    )
    # sum up discount gained to get discount cumulative gain
    df_dcg = df_dcg.groupby(col_user, as_index=False).agg({"dcg": "sum"})
    # calculate ideal discounted cumulative gain
    df_ndcg = df_dcg.merge(df_hit_count, on=[col_user])
    df_ndcg = df_ndcg.apply_rows(
        idcg,
        incols=['actual'],
        outcols=dict(idcg=np.float64),
        kwargs=dict(k=k, log1p=log1p)
    )

    # DCG over IDCG is the normalized DCG
    return (df_ndcg["dcg"] / df_ndcg["idcg"]).sum() / n_users


def map_at_k(
    rating_true,
    rating_pred,
    col_user=DEFAULT_USER_COL,
    col_item=DEFAULT_ITEM_COL,
    col_rating=DEFAULT_RATING_COL,
    col_prediction=DEFAULT_PREDICTION_COL,
    relevancy_method="top_k",
    k=DEFAULT_K,
    threshold=DEFAULT_THRESHOLD,
):
    """Mean Average Precision at k

    Args:
        rating_true (cu.DataFrame): True DataFrame
        rating_pred (cu.DataFrame): Predicted DataFrame
        col_user (str): column name for user
        col_item (str): column name for item
        col_rating (str): column name for rating
        col_prediction (str): column name for prediction
        relevancy_method (str): method for determining relevancy ['top_k', 'by_threshold']
        k (int): number of top k items per user
        threshold (float): threshold of top items per user (optional)

    Returns:
        float: MAP at k (min=0, max=1).
    """

    df_hit, df_hit_count, n_users = merge_ranking_true_pred(
        rating_true=rating_true,
        rating_pred=rating_pred,
        col_user=col_user,
        col_item=col_item,
        col_prediction=col_prediction,
        relevancy_method=relevancy_method,
        k=k,
        threshold=threshold,
    )

    if df_hit.shape[0] == 0:
        return 0.0

    # calculate reciprocal rank of items for each user and sum them up
    df_hit_sorted = df_hit.sort_values([col_user, "rank"])
    df_hit_sorted["rr"] = (df_hit.groupby(col_user).cumcount() + 1) / df_hit["rank"]
    df_hit_sorted = df_hit_sorted.groupby(col_user).agg({"rr": "sum"}).reset_index()

    df_merge = pd.merge(df_hit_sorted, df_hit_count, on=col_user)
    return (df_merge["rr"] / df_merge["actual"]).sum() / n_users
