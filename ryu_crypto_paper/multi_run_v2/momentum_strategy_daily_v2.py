import numpy as np
import pandas as pd 
import ray

from multi_run_v2.backtest_pf_v2 import simulate_longonly, simulate_longshort   # 벡테스팅 버전 바뀌면 여기를 수정해야함
from multi_run_v2.initialize_v2 import inner_data_pp

ray.init(num_cpus=16, ignore_reinit_error=True)

######################## - V3 - 2023-05-15 Edited ######################
# 1. Daily로 시그널 계산
# 2023-06-12 Capped 방법 수정
# ######################################################################

@ ray.remote 
def weekly_momentum_value_weighted(price_df:pd.DataFrame, mktcap_df:pd.DataFrame, vol_df:pd.DataFrame,
                                   n_group:int, day_of_week:str, number_of_coin_group:int, mktcap_thresh=None, vol_thresh=None,
                                   fee_rate=0, look_back=14):
    '''
    Value Weighted로 Cross-Sectional Momentum 투자 비중을 생성합니다
    
        group_num : 몇 개의 그룹으로 나눌 지
        day_of_week : Rebalancing을 진행할 요일 [MON,TUE,WED,THU,FRI,SAT,SUN]
        number_of_coin_group : 그룹당 최소 필요한 코인 수
        mktcap_value : mktcap_value 이하는 스크리닝 (MA30)
        vol_value : vol_value 이하는 스크리닝 (MA30)
        fee_rate : 거래비용
        look_back : Default 7
    
    Return -> final_value, group_coin_count
    '''
    
    #group_coin_count = {}
    final_value = {}
    group_weight_list = []

    daily_rtn_df, mktcap_used, group_mask_dict, mask = inner_data_pp(price_df, mktcap_df, vol_df, n_group, day_of_week, 
                                                                     number_of_coin_group, mktcap_thresh, vol_thresh, look_back=look_back)  # initialize_v2에 있음
    for key, mask in group_mask_dict.items():
        # 그룹의 value weighted weight 생성
        group_weight = (mktcap_used * mask).apply(lambda x: x / np.nansum(x), axis=1)
        #group_coin_count[key] = group_weight.count(1)
        group_weight_list.append(group_weight)   # v5 추가(롱숏 계산을 위해)
        # 투자 성과를 측정합니다
        final_value["Long_" + str(key)] = simulate_longonly(group_weight_df=group_weight, daily_rtn_df=daily_rtn_df, fee_rate=fee_rate)
             
    # 롱숏 계산 
    final_value["LS-cross"] = simulate_longshort(long_weight_df=group_weight_list[-1], short_weight_df=group_weight_list[0],
                                                 daily_rtn_df=daily_rtn_df, fee_rate=fee_rate, margin="cross")
    final_value["LS-isolate"] = simulate_longshort(long_weight_df=group_weight_list[-1], short_weight_df=group_weight_list[0],
                                                 daily_rtn_df=daily_rtn_df, fee_rate=fee_rate, margin="isolate")
    
    return final_value  # group_coin_count

    
@ray.remote
def weekly_momentum_value_weighted_capped(price_df:pd.DataFrame, mktcap_df:pd.DataFrame, vol_df:pd.DataFrame,
                                          n_group:int, day_of_week:str, number_of_coin_group:int,
                                          mktcap_thresh=None, vol_thresh=None, fee_rate=0, num_cap=0.95, look_back=7):
    '''
    Value Weighted로 투자 비중을 생성합니다 (상위 num_cap만큼 marketcap을 제한 해준다)
    
        n_group : 몇 개의 그룹으로 나눌 지
        day_of_week : Rebalancing을 진행할 요일 [MON,TUE,WED,THU,FRI,SAT,SUN]
        number_of_coin_group : 그룹당 최소 필요한 코인 수
        mktcap_thresh : mktcap_thresh 이하는 스크리닝 (MA30)
        vol_thresh : vol_thresh 이하는 스크리닝 (MA30)
        fee_rate : 거래비용
        num_cap : cap을 씌울 코인 개수 (Ex:5일 경우 그룹별로 Marketcap 상위 5개의 코인의 weight를 가장 낮은 weight로 맞춥니다)
        look_back : Default 7

        Return -> final_value, group_coin_count
    '''
    #group_coin_count = {}
    final_value = {}
    group_weight_list = []   

    daily_rtn_df, mktcap_used, group_mask_dict, mask = inner_data_pp(price_df=price_df, mktcap_df=mktcap_df,
                                                                     vol_df=vol_df, n_group=n_group, 
                                                                     day_of_week=day_of_week, number_of_coin_group=number_of_coin_group,
                                                                     mktcap_thresh=mktcap_thresh,vol_thresh=vol_thresh,
                                                                     look_back=look_back)  # initialize_v2에 있음
    
    # Capped 씌워주기 (전체에서 Cap을 씌우고, 그룹을 나눠준다)
    mktcap_df_used = mktcap_used.copy()
    mktcap_rank = mktcap_df_used.rank(1)
    rank_thresh = (mktcap_rank.max(1) * num_cap).dropna().map(int) 
    
    # index alignment
    mktcap_rank = mktcap_rank.loc[rank_thresh.index]
    coin_thresh_series = mktcap_rank.eq(rank_thresh, axis=0).idxmax(axis=1) # values가 타겟 코인의 컬럼명, index가 날짜인 시리즈
    filtered = mktcap_rank.apply(lambda x: x > rank_thresh, axis=0)
    
    for date, target_col in coin_thresh_series.items(): # for loop를 돌면서 mktcap을 변경합니다
        target = mktcap_df_used.loc[date, target_col]
        mask_row = filtered.loc[date]
        mktcap_df_used.loc[date, mask_row] = target 
    
    for key, mask in group_mask_dict.items():   
        # 그룹의 marketcap 개산
        group_mktcap = (mktcap_df_used * mask)

        # Weight를 계산
        group_weight = group_mktcap.apply(lambda x: x / np.nansum(x), axis=1)
        #group_coin_count[key] = group_weight.count(1)
        group_weight_list.append(group_weight)   # v5 추가(롱숏 계산을 위해)            
        # 투자 성과를 측정합니다
        final_value["Long_" + str(key)] = simulate_longonly(group_weight_df=group_weight, daily_rtn_df=daily_rtn_df, fee_rate=fee_rate)
            
    # 롱숏 계산 
    final_value["LS-cross"], dv_df = simulate_longshort(long_weight_df=group_weight_list[-1], short_weight_df=group_weight_list[0],
                                                 daily_rtn_df=daily_rtn_df, fee_rate=fee_rate, margin="cross")
    #final_value["LS-isolate"]  = simulate_longshort(long_weight_df=group_weight_list[-1], short_weight_df=group_weight_list[0],
    #                                               daily_rtn_df=daily_rtn_df, fee_rate=fee_rate, margin="isolate")
    
    return final_value, dv_df  # group_coin_count




@ray.remote
def weekly_momentum_volume_weighted_capped(price_df:pd.DataFrame, mktcap_df:pd.DataFrame, vol_df:pd.DataFrame,
                                           n_group:int, day_of_week:str, number_of_coin_group:int,
                                           mktcap_thresh=None, vol_thresh=None, fee_rate=0, num_cap=0.95, look_back=7):
    '''
    Value Weighted로 투자 비중을 생성합니다 (상위 num_cap만큼 marketcap을 제한 해준다)
    
        n_group : 몇 개의 그룹으로 나눌 지
        day_of_week : Rebalancing을 진행할 요일 [MON,TUE,WED,THU,FRI,SAT,SUN]
        number_of_coin_group : 그룹당 최소 필요한 코인 수
        mktcap_thresh : mktcap_thresh 이하는 스크리닝 (MA30)
        vol_thresh : vol_thresh 이하는 스크리닝 (MA30)
        fee_rate : 거래비용
        num_cap : cap을 씌울 코인 개수 (Ex:5일 경우 그룹별로 Marketcap 상위 5개의 코인의 weight를 가장 낮은 weight로 맞춥니다)
        look_back : Default 7

        Return -> final_value, group_coin_count
    '''
    #group_coin_count = {}
    final_value = {}
    group_weight_list = []   

    daily_rtn_df, mktcap_used, group_mask_dict = inner_data_pp(price_df=price_df, mktcap_df=mktcap_df,
                                                               vol_df=vol_df, n_group=n_group, 
                                                               day_of_week=day_of_week, number_of_coin_group=number_of_coin_group,
                                                               mktcap_thresh=mktcap_thresh,vol_thresh=vol_thresh,
                                                               look_back=look_back)  # initialize_v2에 있음
    
    # Capped 씌워주기 (전체에서 Cap을 씌우고, 그룹을 나눠준다)
    volume_df_used = vol_df.copy()
    volume_rank = volume_df_used.rank(1)
    rank_thresh = (volume_rank.max(1) * num_cap).dropna().map(int)
    
    # index alignment
    volume_rank = volume_rank.loc[rank_thresh.index]
    coin_thresh_series = volume_rank.eq(rank_thresh, axis=0).idxmax(axis=1) # values가 타겟 코인의 컬럼명, index가 날짜인 시리즈
    filtered = volume_rank.apply(lambda x: x > rank_thresh, axis=0)
    
    for date, target_col in coin_thresh_series.items(): # for loop를 돌면서 mktcap을 변경합니다
        target = volume_df_used.loc[date, target_col]
        mask_row = filtered.loc[date]
        volume_df_used.loc[date, mask_row] = target 
    
    for key, mask in group_mask_dict.items():   
        # 그룹의 marketcap 개산
        group_mktcap = (volume_df_used * mask)

        # Weight를 계산
        group_weight = group_mktcap.apply(lambda x: x / np.nansum(x), axis=1)
        #group_coin_count[key] = group_weight.count(1)
        group_weight_list.append(group_weight)   # v5 추가(롱숏 계산을 위해)            
        # 투자 성과를 측정합니다
        final_value["Long_" + str(key)] = simulate_longonly(group_weight_df=group_weight, daily_rtn_df=daily_rtn_df, fee_rate=fee_rate)
            
    # 롱숏 계산 
    final_value["LS-cross"], dv_df = simulate_longshort(long_weight_df=group_weight_list[-1], short_weight_df=group_weight_list[0],
                                                 daily_rtn_df=daily_rtn_df, fee_rate=fee_rate, margin="cross")
    #final_value["LS-isolate"]  = simulate_longshort(long_weight_df=group_weight_list[-1], short_weight_df=group_weight_list[0],
    #                                               daily_rtn_df=daily_rtn_df, fee_rate=fee_rate, margin="isolate")
    
    return final_value, dv_df  # group_coin_count