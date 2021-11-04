import numpy as np
import pandas as pd
import os



flag = True
path = 'D:\\Github\\Spike-Element-Wise-ResNet\\dvsgesture\\firing'
layers = 15

all = []
for idx in range(0, 288):
    name = str(idx) + '.csv'
    df = pd.read_csv(os.path.join(path, name), header=None).values
    if flag:
        for layer in range(layers):
            all.append(df[layer + 1 , 1:])
        flag = False
    else:
        for layer in range(layers):
            all[layer] = all[layer] + df[layer + 1 , 1:]

list = []
for nums in all:
    sub_list = []
    num = 0
    sum = 0
    for idx in range(len(nums) - 1):
        num += nums[idx]
        sum += nums[len(nums) - 1]
        sub_list.append(nums[idx] / nums[len(nums) - 1])
    sub_list.append(num / sum)
    list.append(sub_list)

csv = pd.DataFrame(
    data=list
)
csv.to_csv('./all.csv')