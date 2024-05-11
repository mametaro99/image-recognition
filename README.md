
Webカメラで人の顔を認識して、輪郭・目・鼻などを描画するプログラムです。



## 動かし方
リポジトリをダウンロードします。

```
git clone git@github.com:mametaro99/image-recognition.git
```
以下のコマンドを入力してください。
```
apt update
apt install -y python3-pip libgl1-mesa-dev libglib2.0-0
pip install aiohttp aiortc opencv-python opencv-contrib-python websockets
```

ルートディレクトリから移動して、サーバを起動
```
/examples/server
python3 server.py
```

## 実装結果

![無題の動画 ‐ Clipchampで作成 (1)](https://github.com/mametaro99/image-recognition/assets/141534298/07434304-f9b1-472c-88cc-0b2252f24915)


## 工夫したところ

## 改善点
- 計算の処理が重くて、徐々に動作が重くなってしまう。
- chatgptに書かせても、上手くいかず、解決策が分からずに時間がかかった。参考記事を見て、コードを作る方がが機械学習やコードの理解することができ、今後の開発に役立ちそう。
## 参考
- OpenCVの新しい顔検出を試してみる
https://qiita.com/UnaNancyOwen/items/f3db189760037ec680f3


- WebRTC+Pythonを用いたリモート・リアルタイム映像処理開発方法の紹介
https://knowledge.sakura.ad.jp/29752/
