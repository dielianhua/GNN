import argparse
import os.path as osp
import random
import warnings
from time import time, sleep

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import torch
import torch.nn.functional as F
from sklearn import cluster as cl
from sklearn import metrics as me
from sklearn import neighbors
from sklearn.metrics import classification_report
from sklearn.metrics.pairwise import pairwise_distances
from torch.nn import Sequential as Seq, Linear as Lin, ReLU, BatchNorm1d as BN, Dropout
from torch_geometric.data import Data
from torch_geometric.datasets import Planetoid, Reddit
from torch_geometric.nn import GCNConv, GATConv, GAE, VGAE, ARMAConv, AGNNConv, ARGA, ARGVA
from torch_geometric.utils import subgraph, to_networkx
from xgboost import XGBClassifier

from pytorchtools import EarlyStopping
from sklearn.metrics import roc_auc_score, average_precision_score

warnings.filterwarnings('ignore')

early = True  # 是否启用early stop
link_test = False  # 是否进行传统方法link test任务以便进行对比
su_test = False  # 是否对model进行supervised测试（classification）
un_test = False  # 是否对model进行unsupervised测试（clustering）
complete = False  # 是否进行matrix completion（对每对节点进行link prediction）
show_plot = False  # 是否利用生成graph的图示（很耗时间）

plt.rcParams['savefig.dpi'] = 300  # 图片像素
plt.rcParams['figure.dpi'] = 300  # 分辨率

parser = argparse.ArgumentParser()
parser.add_argument('--model', type=str, default='GAE')
parser.add_argument('--dataset', type=str, default='Cora')
parser.add_argument('--encoder', type=str, default='GCN')
parser.add_argument('--dropout', type=float, default=0.5)
parser.add_argument('--lr', type=float, default=1e-3)
parser.add_argument('--l2', type=float, default=0)
parser.add_argument('--dis_loss_para', type=float, default=1)
parser.add_argument('--reg_loss_para', type=float, default=1)
parser.add_argument('--epoch', type=int, default=1000)
parser.add_argument('--hidden_channels', type=int, default=2)
parser.add_argument('--patience', type=int, default=50)
parser.add_argument('--subgraph_num', type=int, default=1)
parser.add_argument('--batch', type=int, default=1024)
args = parser.parse_args()
checkpoint_filename = f'checkpoint_{args.model}_{args.encoder}_{args.dataset}.pt'
kwargs = {'GAE': GAE, 'VGAE': VGAE, 'ARGA': ARGA, 'ARGVA': ARGVA}
encoder_args = {'GCN': GCNConv, 'GAT': GATConv, 'ARMA': ARMAConv, 'AGNN': AGNNConv}
assert args.model in kwargs.keys()
assert args.encoder in encoder_args.keys()
assert args.dataset in ['Cora', 'CiteSeer', 'PubMed', 'Reddit']


def MLP(channels):
    return Seq(*[
        Seq(Lin(channels[i - 1], channels[i]), ReLU(), BN(channels[i]))
        for i in range(1, len(channels))
    ])


class Encoder(torch.nn.Module):
    def __init__(self, in_channels, out_channels):
        super(Encoder, self).__init__()
        if args.encoder in ['GCN']:
            self.conv1 = GCNConv(in_channels, 2 * out_channels)
        elif args.encoder in ['GAT']:
            self.conv1 = GATConv(in_channels, 32, heads=4, dropout=args.dropout)
        elif args.encoder in ['ARMA']:
            self.conv1 = ARMAConv(
                in_channels,
                2 * out_channels,
                num_stacks=3,
                num_layers=2,
                shared_weights=True,
                dropout=args.dropout)
        elif args.encoder in ['AGNN']:
            self.lin1 = torch.nn.Linear(in_channels, 2 * out_channels)
            self.lin2 = torch.nn.Linear(2 * out_channels, out_channels)

            self.conv1 = AGNNConv(requires_grad=False)

        if args.model in ['GAE', 'ARGA']:
            if args.encoder in ['GCN']:
                self.conv2 = GCNConv(2 * out_channels, out_channels)
            elif args.encoder in ['GAT']:
                self.conv2 = GATConv(8 * 16, out_channels, dropout=args.dropout)
            elif args.encoder in ['ARMA']:
                self.conv2 = ARMAConv(
                    2 * out_channels,
                    out_channels,
                    num_stacks=3,
                    num_layers=2,
                    shared_weights=True,
                    dropout=0.25,
                    act=None)
            elif args.encoder in ['AGNN']:
                self.conv2 = AGNNConv(requires_grad=True)
        elif args.model in ['VGAE', 'ARGVA']:
            if args.encoder in ['GCN']:
                self.conv_mu = GCNConv(2 * out_channels, out_channels)
                self.conv_logvar = GCNConv(
                    2 * out_channels, out_channels)
            elif args.encoder in ['GAT']:
                self.conv_mu = GATConv(8 * 8, out_channels, dropout=args.dropout)
                self.conv_logvar = GATConv(8 * 8, out_channels, dropout=args.dropout)
            elif args.encoder in ['ARMA']:
                self.conv_mu = ARMAConv(
                    2 * out_channels,
                    out_channels,
                    num_stacks=3,
                    num_layers=2,
                    shared_weights=True,
                    dropout=0.25,
                    act=None)
                self.conv_logvar = ARMAConv(
                    2 * out_channels,
                    out_channels,
                    num_stacks=3,
                    num_layers=2,
                    shared_weights=True,
                    dropout=0.25,
                    act=None)
            elif args.encoder in ['AGNN']:
                self.lin3 = torch.nn.Linear(2 * out_channels, out_channels)
                self.conv_mu = AGNNConv(requires_grad=True)
                self.conv_logvar = AGNNConv(requires_grad=True)

    def forward(self, x, edge_index):
        x = F.dropout(x, p=args.dropout, training=self.training)
        if args.encoder in ['AGNN']:
            x = F.relu(self.lin1(x))
        x = self.conv1(x, edge_index)
        if args.encoder not in ['AGNN']:
            x = F.relu(x)
        if args.model in ['GAE', 'ARGA']:
            x = self.conv2(x, edge_index)
            if args.encoder in ['AGNN']:
                x = F.dropout(x, training=self.training, p=args.dropout)
                x = self.lin2(x)
            return x
        elif args.model in ['VGAE', 'ARGVA']:
            mu = self.conv_mu(x, edge_index)
            logvar = self.conv_logvar(x, edge_index)
            if args.encoder in ['AGNN']:
                mu = F.dropout(mu, training=self.training, p=args.dropout)
                mu = self.lin2(mu)
                logvar = F.dropout(logvar, training=self.training, p=args.dropout)
                logvar = self.lin2(logvar)
            return mu, logvar


class Discriminator(torch.nn.Module):
    def __init__(self, in_channels, out_channels=1):
        super(Discriminator, self).__init__()
        self.mlp = Seq(
            MLP([in_channels, 128, 64]), Dropout(args.dropout),
            Lin(64, out_channels))

    def forward(self, x):
        return self.mlp(x)


def train(data):
    print('--------------------------------------------------\n\n')
    print('Train:')
    global epoch, learning_rate, weight_decay, model
    if early:
        early_stopping = EarlyStopping(patience=args.patience, filename=checkpoint_filename)
    best_roc = 0
    best_mean = 0
    for epoch in range(args.epoch):
        model.train()
        optimizer.zero_grad()
        z = model.encode(data.x, data.train_pos_edge_index)
        loss = model.recon_loss(z, data.train_pos_edge_index)
        if early:
            test_loss = model.recon_loss(z, data.test_pos_edge_index)
        if args.model in ['VGAE', 'ARGVA']:
            loss = loss + (1 / data.num_nodes) * model.kl_loss()
            if early:
                test_loss = test_loss + (1 / data.num_nodes) * model.kl_loss()
        if args.model in ['ARGA', 'ARGVA']:
            loss = loss + args.dis_loss_para * model.discriminator_loss(z) + args.reg_loss_para * model.reg_loss(z)
            if early:
                test_loss = test_loss + args.dis_loss_para * model.discriminator_loss(
                    z) + args.reg_loss_para * model.reg_loss(z)
        loss.backward()
        optimizer.step()
        if not epoch % 5:
            model.eval()
            with torch.no_grad():
                z = model.encode(data.x, data.train_pos_edge_index)
                roc, mean = model.test(z, data.test_pos_edge_index, data.test_neg_edge_index)
                best_roc = roc if roc > best_roc else best_roc
                best_mean = mean if mean > best_mean else best_mean
                print(f'graph_num:{graph_num}\tepoch:{epoch}\tloss:{loss}\troc:{roc}\tmean:{mean}')
        if early:
            early_stopping(test_loss, model)
            if early_stopping.early_stop:
                print("Early stop!")
                break
    if early:
        model.load_state_dict(torch.load(checkpoint_filename))
        print(f'Best loss:{-early_stopping.best_score}\nBest roc:{best_roc}\nBest mean auc:{best_mean}')


def test_unsupervised(model, data):
    print('\n\n--------------------------------------------------')
    print('Unsupervised test...')
    cluster_model = cl.KMeans(data.y.cpu().numpy().max() + 1)

    model.eval()
    z = model.encode(data.x, data.train_pos_edge_index)
    z = z.cpu().detach().numpy()
    if data.y is None:
        cluster_model.fit(z)
    else:
        true_label = data.y.cpu().detach().numpy()

        t = time()
        predict_label = cluster_model.fit_predict(data.x.cpu().detach().numpy())
        t_before = time() - t
        X = pairwise_distances(data.x.cpu().detach().numpy(), metric='euclidean')
        before_embedding_1 = (true_label, predict_label)
        before_embedding_2 = (X, true_label)

        t = time()
        predict_label = cluster_model.fit_predict(z)
        t_after = time() - t
        X = pairwise_distances(z, metric='euclidean')

        after_embedding_1 = (true_label, predict_label)
        after_embedding_2 = (X, true_label)
        metric_before = {'time': t_before,
                         'adjusted_rand_score': me.adjusted_rand_score(*before_embedding_1),
                         'adjuested_mutual_info_score': me.adjusted_mutual_info_score(*before_embedding_1),
                         'homogeneity_score': me.homogeneity_score(*before_embedding_1),
                         'completeness_score': me.completeness_score(*before_embedding_1),
                         'v_measure_score': me.v_measure_score(*before_embedding_1),
                         'fowlkes_mallows_score': me.fowlkes_mallows_score(*before_embedding_1),
                         'silhouette_score': me.silhouette_score(*before_embedding_2),
                         'calinski_harabaz_score': me.calinski_harabaz_score(*before_embedding_2)}

        metric_after = {'time': t_after,
                        'adjusted_rand_score': me.adjusted_rand_score(*after_embedding_1),
                        'adjuested_mutual_info_score': me.adjusted_mutual_info_score(*after_embedding_1),
                        'homogeneity_score': me.homogeneity_score(*after_embedding_1),
                        'completeness_score': me.completeness_score(*after_embedding_1),
                        'v_measure_score': me.v_measure_score(*after_embedding_1),
                        'fowlkes_mallows_score': me.fowlkes_mallows_score(*after_embedding_1),
                        'silhouette_score': me.silhouette_score(*after_embedding_2),
                        'calinski_harabaz_score': me.calinski_harabaz_score(*after_embedding_2)}
        print('\n\n\nTest:')
        print('--------------------------------------------------\n\n')
        print('Before embedding:')
        for key, value in metric_before.items():
            print(f'{key}:{value}')
        print('--------------------------------------------------\n\n')
        print('After embedding:')
        for key, value in metric_after.items():
            print(f'{key}:{value}')

    return cluster_model, z


def test_supervised_classification(model, data):
    print('\n\n--------------------------------------------------')
    print('Supervised classification test...')
    model.eval()
    z = model.encode(data.x, data.train_pos_edge_index)
    train_label = data.y[data.train_mask].cpu().detach().numpy()
    test_label = data.y[data.test_mask].cpu().detach().numpy()
    target_names = [f'class{i}' for i in range(data.y.cpu().numpy().max() + 1)]

    print('\n\n\nNormal classifier')
    classify_model = neighbors.KNeighborsClassifier()
    classify_model.fit(data.x[data.train_mask].cpu().detach().numpy(), train_label)

    ori_pre = classify_model.predict(data.x[data.test_mask].cpu().detach().numpy())
    print('--------------------------------------------------\n\n')
    print('Original:')
    print(classification_report(test_label, ori_pre, target_names=target_names))

    classify_model = neighbors.KNeighborsClassifier()
    classify_model.fit(z[data.train_mask].cpu().detach().numpy(), train_label)
    ori_pre = classify_model.predict(z[data.test_mask].cpu().detach().numpy())
    print('--------------------------------------------------\n\n')
    print('Embedded:')
    print(classification_report(test_label, ori_pre, target_names=target_names))

    print('\n\n\nStrong classifier')
    classify_model = XGBClassifier(tree_method='gpu_hist', predictor='gpu_predictor')
    classify_model.fit(data.x[data.train_mask].cpu().detach().numpy(), train_label)
    ori_pre = classify_model.predict(data.x[data.test_mask].cpu().detach().numpy())
    print('--------------------------------------------------\n\n')
    print('Original:')
    print(classification_report(test_label, ori_pre, target_names=target_names))

    classify_model = XGBClassifier(tree_method='gpu_hist', predictor='gpu_predictor')
    # classify_model = neighbors.KNeighborsClassifier()
    classify_model.fit(z[data.train_mask].cpu().detach().numpy(), train_label)
    ori_pre = classify_model.predict(z[data.test_mask].cpu().detach().numpy())
    print('--------------------------------------------------\n\n')
    print('Embedded:')
    print(classification_report(test_label, ori_pre, target_names=target_names))


def link_prediction_test(data):
    print('\n\n--------------------------------------------------')
    print('Supervised link prediction test...')
    pos_x = torch.cat([data.x.index_select(0, data.train_pos_edge_index[i, :]) for i in range(2)],
                      1).cpu().detach().numpy()
    pos_y = np.array([1 for i in range(pos_x.shape[0])])
    neg_x = torch.cat([data.x.index_select(0, data.test_neg_edge_index[i, :].cuda()) for i in range(2)],
                      1).cpu().detach().numpy()
    neg_y = np.array([0 for i in range(neg_x.shape[0])])

    train_x = np.concatenate([pos_x, neg_x], 0)
    train_y = np.concatenate([pos_y, neg_y], 0)

    pos_x = torch.cat([data.x.index_select(0, data.test_pos_edge_index[i, :]) for i in range(2)],
                      1).cpu().detach().numpy()
    pos_y = np.array([1 for i in range(pos_x.shape[0])])
    neg_x = torch.cat([data.x.index_select(0, data.val_neg_edge_index[i, :].cuda()) for i in range(2)],
                      1).cpu().detach().numpy()
    neg_y = np.array([0 for i in range(neg_x.shape[0])])
    test_x = np.concatenate([pos_x, neg_x], 0)
    test_y = np.concatenate([pos_y, neg_y], 0)

    print('\n\n\nNormal classifier')
    classify_model = neighbors.KNeighborsClassifier()
    classify_model.fit(train_x, train_y)
    pre_y = classify_model.predict(test_x)

    print('--------------------------------------------------\n\n')
    print(f'roc:{roc_auc_score(test_y, pre_y)}\tmean:{average_precision_score(test_y, pre_y)}')

    print('\n\n\nStrong classifier')
    classify_model = XGBClassifier(tree_method='gpu_hist', predictor='gpu_predictor')
    classify_model.fit(train_x, train_y)
    pre_y = classify_model.predict(test_x)

    print('--------------------------------------------------\n\n')
    print(f'roc:{roc_auc_score(test_y, pre_y)}\tmean:{average_precision_score(test_y, pre_y)}')


def plot_graph(cluster_model, z):
    print('\n\n--------------------------------------------------')
    print('Ploting...')
    label_pred = cluster_model.labels_  # 获取聚类标签
    mark = ['b', 'g', 'r', 'c', 'm', 'y', 'k', 'w']
    c_list = [mark[i] for i in label_pred]
    if z.shape[1] == 2:
        ax = plt.subplot(111)
        ax.scatter(z[:, 0], z[:, 1], c=c_list, s=10)
        ax.set_ylabel('Y')
        ax.set_xlabel('X')
    elif z.shape[1] == 3:
        from mpl_toolkits.mplot3d import Axes3D
        ax = plt.subplot(111, projection='3d')  # 创建一个三维的绘图工程
        ax.scatter(z[:, 0], z[:, 1], z[:, 2], c=c_list, s=20)
        ax.set_zlabel('Z')
        ax.set_ylabel('Y')
        ax.set_xlabel('X')

    else:
        print('Wrong dimension!')
    plt.show()


def complete_graph(model, data, num_nodes=None, cluster_model=None):
    print('\n\n--------------------------------------------------')
    print('Completing graph...')
    t = time()
    if num_nodes is None:
        num_nodes = data.num_nodes

    g = to_networkx(data)
    old_edge_num = g.number_of_edges()
    data.to(dev)
    model.to(dev)
    draw_args = {'G': g, 'with_labels': False, 'pos': nx.spring_layout(g), 'node_size': 5}
    if cluster_model is not None:
        label_pred = cluster_model.labels_  # 获取聚类标签
        mark = ['b', 'g', 'r', 'c', 'm', 'y', 'k', 'w']
        c_list = [mark[i] for i in label_pred]
        draw_args['node_color'] = c_list
    if show_plot:
        nx.draw(**draw_args)
        # plt.savefig('old.eps',  format='eps')
        plt.show()
    whole_edge_test = torch.LongTensor(
        [[i % num_nodes for i in range(num_nodes ** 2)], [j // num_nodes for j in range(num_nodes ** 2)]])
    z = model.encode(data.x, data.edge_index)
    hh = []
    for i in range(0, num_nodes ** 2, args.batch):
        input = whole_edge_test[:, i:min(i + args.batch, num_nodes ** 2)].to(dev)
        hh.append(model.decoder(z, input, sigmoid=True).detach().cpu())
    sig = torch.cat(hh)
    # sig = model.decoder(z, whole_edge_test, sigmoid=True)
    category_mask = torch.gt(sig, 0.5)
    new_edge = whole_edge_test[:, category_mask].t().numpy()
    g.add_edges_from(new_edge)
    new_edge_num = g.number_of_edges()
    print(
        f'Original edge numbers:{old_edge_num}\t Completed edge numbers:{new_edge_num}\tIncrement:{new_edge_num - old_edge_num}')
    if show_plot:
        draw_args['pos'] = nx.spring_layout(g)
        draw_args['G'] = g
        nx.draw(**draw_args)
        # plt.savefig('new.eps', format='eps')
        plt.show()
    nx.write_gpickle(g, 'complete_graph.gpickle')
    print(f'Used time:{time() - t}')


def generate_subgraph(ori_data):
    if args.subgraph_num > 1:
        x, y, edge_index = None, None, None
        node_list = [i for i in range(ori_data.num_nodes)]
        random.shuffle(node_list)
        node_list = node_list[:ori_data.num_nodes // args.subgraph_num]
        if ori_data.x is not None:
            x = ori_data.x[node_list]
        if ori_data.y is not None:
            y = ori_data.y[node_list]
        if ori_data.edge_index is not None:
            edge_index = subgraph(node_list, ori_data.edge_index, None, True, ori_data.num_nodes)[0]
        data = Data(x=x, y=y, edge_index=edge_index).to(dev)
        data = model.split_edges(data)
    else:
        data = Data(x=ori_data.x, y=ori_data.y, edge_index=ori_data.edge_index).to(dev)
        data = model.split_edges(data)
    return data

dev = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
path = osp.join(
    osp.dirname(osp.realpath(__file__)), '..', 'data', args.dataset)
if args.dataset in ['Cora', 'CiteSeer', 'PubMed']:
    dataset = Planetoid(path, args.dataset)
elif args.dataset in ['Reddit']:
    dataset = Reddit(path)
data = dataset[0]
print(f'Graph information:\nNode:{data.num_nodes}\nEdge:{data.num_edges}\nFeature:{data.num_node_features}')
# 无监督
parameter2model = [Encoder(data.num_features, args.hidden_channels)]
if args.model in ['ARGVA', 'ARGA']:
    dis = Discriminator(args.hidden_channels)
    parameter2model.append(dis)

model = kwargs[args.model](*parameter2model).to(dev)

# 半监督
# model = Encoder(num_feature, 1)
# data = data.to(dev)
if __name__ == '__main__':
    ori_data = data.clone().to(dev)
    for graph_num in range(args.subgraph_num):
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.l2)
        data = generate_subgraph(ori_data)
        train(data)
        print(f'complete {graph_num}th subgraph, sleep 1s...')
        sleep(1)
    ori_data = model.split_edges(ori_data)
    model.eval()
    with torch.no_grad():
        z = model.encode(ori_data.x, ori_data.train_pos_edge_index)
        roc, mean = model.test(z, ori_data.test_pos_edge_index, ori_data.test_neg_edge_index)
    print(f'Final test\tROC:{roc}\tAP:{mean}')
    if link_test:
        link_prediction_test(model.split_edges(ori_data.clone()))
    if su_test:
        test_supervised_classification(model, model.split_edges(ori_data.clone()))
    com_args = {'model': model, 'data': ori_data}
    if un_test:
        cluster_model, z = test_unsupervised(model, model.split_edges(ori_data.clone()))
        plot_graph(cluster_model, z)
        com_args['cluster_model'] = cluster_model

    if complete:
        complete_graph(**com_args, num_nodes=5)
