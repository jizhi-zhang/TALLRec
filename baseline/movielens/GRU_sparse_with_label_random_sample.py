import numpy as np
import pandas as pd
import argparse
import torch
from torch import nn
import torch.nn.functional as F
import os
import logging
import time as Time
from utility import pad_history,calculate_hit
from collections import Counter
import copy
import matplotlib.pyplot as plt
from torch.utils.tensorboard import SummaryWriter   
import random
from sklearn.metrics import roc_auc_score

writer = SummaryWriter('./path/to/log')

logging.getLogger().setLevel(logging.INFO)

def parse_args():
    parser = argparse.ArgumentParser(description="Run supervised GRU.")

    parser.add_argument('--epoch', type=int, default=100,
                        help='Number of max epochs.')
    parser.add_argument('--data', nargs='?', default='yc',
                        help='yc, ks, rr')
    # parser.add_argument('--pretrain', type=int, default=1,
    #                     help='flag for pretrain. 1: initialize from pretrain; 0: randomly initialize; -1: save the model to pretrain file')
    parser.add_argument('--batch_size', type=int, default=256,
                        help='Batch size.')
    parser.add_argument('--hidden_factor', type=int, default=64,
                        help='Number of hidden factors, i.e., embedding size.')
    parser.add_argument('--num_filters', type=int, default=16,
                        help='num_filters')
    parser.add_argument('--filter_sizes', nargs='?', default='[2,3,4]',
                        help='Specify the filter_size')
    parser.add_argument('--r_click', type=float, default=0.2,
                        help='reward for the click behavior.')
    parser.add_argument('--r_buy', type=float, default=1.0,
                        help='reward for the purchase behavior.')
    parser.add_argument('--lr', type=float, default=0.001,
                        help='Learning rate.')
    parser.add_argument('--model_name', type=str, default='Caser_bce',
                        help='model name.')
    parser.add_argument('--save_flag', type=int, default=0,
                        help='0: Disable model saver, 1: Activate model saver')
    parser.add_argument('--cuda', type=int, default=4,
                        help='cuda device.')
    parser.add_argument('--l2_decay', type=float, default=1e-3,
                        help='l2 loss reg coef.')
    parser.add_argument('--alpha', type=float, default=0,
                        help='dro alpha.')
    parser.add_argument('--beta', type=float, default=1.0,
                        help='for robust radius')
    parser.add_argument('--dropout_rate', type=float, default=0.1,
                        help='dropout ')
    parser.add_argument('--descri', type=str, default='',
                        help='description of the work.')
    parser.add_argument("--random_sample", type=int, default=100, help="the random sample num")
    parser.add_argument('--seed', type=int, default=1,
                        help='Random seed.')
    parser.add_argument("--gru_layers", type=int, default=4, help = "the layer num for GRU")
    return parser.parse_args()



class GRU_with_label(nn.Module):
    def __init__(self, hidden_size, item_num, state_size, gru_layers=1):
        super(GRU_with_label, self).__init__()
        self.hidden_size = hidden_size
        self.item_num = item_num
        self.state_size = state_size
        self.item_embeddings = nn.Embedding(
            num_embeddings=item_num + 1,
            embedding_dim=self.hidden_size - 1,
        )
        nn.init.normal_(self.item_embeddings.weight, 0, 0.01)
        self.gru = nn.GRU(
            input_size=self.hidden_size,
            hidden_size=self.hidden_size,
            num_layers=gru_layers,
            batch_first=True
        )
        self.s_fc = nn.Linear(self.hidden_size, self.item_num)

    def forward(self, states, state_rate, len_states):
        # Supervised Head
        emb = self.item_embeddings(states)
        state_rate_reshape = state_rate.view(-1,10,1)
        input_emb = torch.cat((emb, state_rate_reshape), dim=2)
        emb_packed = torch.nn.utils.rnn.pack_padded_sequence(input_emb, len_states, batch_first=True, enforce_sorted=False)
        emb_packed, hidden = self.gru(emb_packed)
        hidden_last = hidden[-1]
        hidden_last = hidden_last.view(-1, hidden_last.shape[-1])
        supervised_output = self.s_fc(hidden_last)
        return supervised_output


def evaluate_auc_with_history_label(model, test_path, device, thresh_list = [0.00005, 0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5]):
    model = model.eval()
    # test_path = "/data/zhangjz/sequential_sparse_v1/test.csv"
    test_data = pd.read_csv(test_path)
    seq, seq_rate, target, target_label = change_train_batch_label(test_data, max_len = 10)
    with torch.no_grad():
        seq = torch.LongTensor(seq)
        target = torch.LongTensor(target)
        seq_rate = torch.LongTensor(seq_rate)
        seq = seq.to(device)
        target = target.to(device)
        seq_rate = seq_rate.to(device)
        len_list = [max_len for q in range(len(seq))]
        model_output = nn.Sigmoid()(model.forward(seq, seq_rate, len_list))
        target = target.view(-1, 1)
        scores = torch.gather(model_output, 1, target)
    scores = scores.view(-1)
    auc = roc_auc_score(target_label, scores.cpu())
    scores_copy = copy.deepcopy(scores)
    acc_list = []
    for thresh in thresh_list:
        orig_scores = copy.deepcopy(scores)
        orig_scores[scores_copy > thresh] = 1
        orig_scores[scores_copy < thresh] = 0
        acc_list.append(torch.sum((orig_scores.cpu() == torch.tensor(target_label))).item()/len(target_label))
    return acc_list, auc


def change_train_batch(batch, max_len):
    history_id = batch["history_movie_id"]
    history_rating = batch["history_rating"]
    target = batch["movie_id"]
    rating = batch["rating"]
    final_history_list= np.array([])
    final_target_list = []
    final_target_label = []
    for key in history_id.keys():
        history_id_temp = np.array(eval(history_id[key]), dtype="int")
        history_rating_temp = np.array(eval(history_rating[key]), dtype="int")
        target_temp = target[key]
        rating_temp = rating[key]
        if sum(history_rating_temp) == 0:
            continue
        pos_his = np.array(history_id_temp[history_rating_temp==1])
        neg_his = np.array(history_id_temp[history_rating_temp==0])
        pad_his_temp = np.pad(pos_his, pad_width=(0, max_len - len(pos_his)), mode="constant", constant_values=pos_his[-1]).reshape(1,-1)
        if len(final_target_list) == 0:
            final_history_list = pad_his_temp
        else:
            final_history_list = np.concatenate((final_history_list, pad_his_temp))
        final_target_list.append(target_temp)
        final_target_label.append(rating_temp)
        if len(neg_his) != 0:
            target_neg = np.random.choice(neg_his)
            final_target_list.append(target_neg)
            final_target_label.append(0)
            final_history_list = np.concatenate((final_history_list, pad_his_temp))
    return final_history_list, final_target_list, final_target_label


def change_train_batch_label(batch, max_len):
    """
    这个是用于考虑当sequential的时候可以将label作为state的一部分输入进去用的
    """
    history_id = batch["history_movie_id"]
    history_rating = batch["history_rating"]
    target = batch["movie_id"]
    rating = batch["rating"]
    final_history_list= np.array([])
    final_target_list = []
    final_target_label = []
    final_history_rate = []
    for key in history_id.keys():
        history_id_temp = np.array(eval(history_id[key]), dtype="int")
        history_rating_temp = np.array(eval(history_rating[key]), dtype="int")
        target_temp = target[key]
        rating_temp = rating[key]
        # if sum(history_rating_temp) == 0:
        #     continue
        # pos_his = np.array(history_id_temp[history_rating_temp==1])
        # neg_his = np.array(history_id_temp[history_rating_temp==0])
        # pad_his_temp = np.pad(pos_his, pad_width=(0, max_len - len(pos_his)), mode="constant", constant_values=pos_his[-1]).reshape(1,-1)
        if len(final_target_list) == 0:
            final_history_list = history_id_temp.reshape(1,-1)
            final_history_rate = history_rating_temp.reshape(1,-1)
        else:
            final_history_list = np.concatenate((final_history_list, history_id_temp.reshape(1,-1)))
            final_history_rate = np.concatenate((final_history_rate, history_rating_temp.reshape(1,-1)))
        final_target_list.append(target_temp)
        final_target_label.append(rating_temp)
    return final_history_list, final_history_rate, final_target_list, final_target_label



def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


if __name__ == '__main__':
    result_list_valid = []
    result_list_test = []
    for sample_num in [2,4,8,16,32,64,128,256,512]:
        args = parse_args()
        setup_seed(args.seed)

        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.cuda)

        # logging.basicConfig(filename="./log/{}/{}_{}_lr{}_decay{}_dro{}_gamma{}_beta{}".format(args.data + '_final2', Time.strftime("%m-%d %H:%M:%S", Time.localtime()), args.model_name, args.lr, args.l2_decay, args.dro_reg, args.gamma, args.beta))
        # Network parameters

        data_directory = './data/' + args.data
        # data_directory = './data/' + args.data
        # data_directory = '../' + args.data + '/data'
        # data_statis = pd.read_pickle(
        #     os.path.join(data_directory, 'data_statis.df'))  # read data statistics, includeing seq_size and item_num

        train_data = pd.read_csv("train.csv")
        train_data = train_data.sample(n=sample_num,random_state=args.seed)
        
        max_len = 10
        seq_size = max_len  # the length of history to define the seq
        item_num = 1683  # total number of items
        thresh_list = [0.00005, 0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5]
        # topk=[10, 20, 50]


        # model = GRU_with_label(args.hidden_factor,item_num, seq_size, args.num_filters, args.filter_sizes, args.dropout_rate)
        model = GRU_with_label(args.hidden_factor,item_num, seq_size, gru_layers = args.gru_layers)
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, eps=1e-8, weight_decay=args.l2_decay)
        mse_loss = nn.MSELoss()

        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        model.to(device)
        # optimizer.to(device)

        # train_data = pd.read_pickle(os.path.join(data_directory, 'train_data.df'))
        # ps = calcu_propensity_score(train_data)
        # ps = torch.tensor(ps)
        # ps = ps.to(device)

        total_step=0
        hr_max = 0
        best_epoch = 0

        num_rows=train_data.shape[0]
        num_batches=int(num_rows/args.batch_size)
        if args.batch_size > num_rows:
            args.batch_size = num_rows
            num_batches = 1
        # best_valid_acc = 0
        # best_test_acc = 0
        best_valid_auc = 0
        best_test_auc = 0
        for i in range(args.epoch):
            for j in range(num_batches):
                model = model.train()
                batch = train_data.sample(n=args.batch_size).to_dict()
                # seq = list(batch['seq'].values())
                # len_seq = list(batch['len_seq'].values())
                # target=list(batch['next'].values())

                # target_neg = []
                # for index in range(args.batch_size):
                #     neg=np.random.randint(item_num)
                #     while neg==target[index]:
                #         neg = np.random.randint(item_num)
                #     target_neg.append(neg)

                seq, seq_rate, target, target_label = change_train_batch_label(batch, max_len=max_len)
                optimizer.zero_grad()
                seq = torch.LongTensor(seq)
                seq_rate = torch.FloatTensor(seq_rate)
                # len_seq = torch.LongTensor(len_seq)
                target = torch.LongTensor(target)
                # target_neg = torch.LongTensor(target_neg)
                seq = seq.to(device)
                target = target.to(device)
                seq_rate = seq_rate.to(device)
                # len_seq = len_seq.to(device)
                # target_neg = target_neg.to(device)

                len_list = [max_len for q in range(args.batch_size)]

                model_output = model.forward(seq, seq_rate, len_list)


                target = target.view(-1, 1)

                scores = torch.gather(model_output, 1, target)
                # neg_scores = torch.gather(model_output, 1, target_neg)

                # pos_labels = torch.ones((args.batch_size, 1))
                # neg_labels = torch.zeros((args.batch_size, 1))

                # scores = torch.cat((pos_scores, neg_scores), 0)
                # labels = torch.cat((pos_labels, neg_labels), 0)
                labels = torch.tensor(target_label).to(device).view(-1,1)

                scores = nn.Sigmoid()(scores)

                loss = mse_loss(scores, labels.float())

                # pos_scores_dro = torch.gather(torch.mul(model_output * model_output, ps), 1, target)
                # pos_scores_dro = torch.squeeze(pos_scores_dro)
                # pos_loss_dro = torch.gather(torch.mul((model_output - 1) * (model_output - 1), ps), 1, target)
                # pos_loss_dro = torch.squeeze(pos_loss_dro)

                # inner_dro = torch.sum(torch.exp((torch.mul(model_output * model_output, ps) / args.beta)), 1) - torch.exp((pos_scores_dro / args.beta)) + torch.exp((pos_loss_dro / args.beta)) 

                # # A = torch.sum(torch.exp(torch.mul(model_output * model_output, ps)), 1)
                # # B = torch.exp(pos_scores_dro)
                # # C = torch.exp(pos_loss_dro) 
                # # print(A.shape, B.shape, C.shape)

                # loss_dro = torch.log(inner_dro + 1e-24)
                # if args.alpha == 0.0:
                #     loss_all = loss
                # else:
                #     loss_all = loss + args.alpha * torch.mean(loss_dro)
                loss.backward()
                optimizer.step()

                if True:

                    total_step+=1
                    # if total_step % 20 == 0:
                    #     # print("the loss in %dth step is: %f" % (total_step, loss))
                    #     pass
                    #     # logging.info("the loss in %dth step is: %f" % (total_step, loss_all))

                    if total_step % 5 == 0:
                            # print('VAL:')
                            # logging.info('VAL:')
                            # hr_20 = evaluate(model, 'val_sessions_pos.df', device)
                            # print('VAL PHRASE:')
                            # logging.info('VAL PHRASE:')
                            valid_acc_list, valid_auc = evaluate_auc_with_history_label(model, '/data/zhangjz/sequential_sparse_v1/valid.csv', device, thresh_list=thresh_list)
                            for (id, item) in enumerate(valid_acc_list):
                                writer.add_scalar('val thresh:' + str(thresh_list[id]), item, global_step=total_step, walltime=None)
                            # print(valid_acc_list)
                            
                            # print('TEST PHRASE:')
                            # logging.info('TEST PHRASE:')
                            test_acc_list, test_auc = evaluate_auc_with_history_label(model, '/data/zhangjz/sequential_sparse_v1/test.csv', device, thresh_list=thresh_list)
                            writer.add_scalar('test thresh:' + str(thresh_list[id]), item, global_step=total_step, walltime=None)

                            # print(test_acc_list)
                            # if best_valid_acc < valid_acc_list[3]:
                            #     best_valid_acc = valid_acc_list[3]
                            #     best_test_acc = test_acc_list[3]

                            if best_valid_auc < valid_auc:
                                best_valid_auc = valid_auc
                                best_test_auc = test_auc
                            
                            print("Best test auc:" + str(best_test_auc))

                            # print('TEST PHRASE3:')
                            # logging.info('TEST PHRASE3:')
                            # _ = evaluate(model, 'test_sessions3_pos.df', device)

                            # if hr_20 > hr_max:

                            #     hr_max = hr_20
                            #     best_epoch = total_step
                            
                            # print('BEST EPOCH:{}'.format(best_epoch))
                            # logging.info('BEST EPOCH:{}'.format(best_epoch))
        result_list_valid.append(best_valid_auc)
        result_list_test.append(best_test_auc)
        print("valid")
        result_list_valid
        print("test")
        result_list_test


                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     
