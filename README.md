# 四大报刊数据库网站

包含国际出版周报、中国新闻出版广电报、中华读书报、文艺报2021年至今的全部文章数据。

## 网站访问

启用 GitHub Pages 后，访问：
```
https://你的用户名.github.io/仓库名/
```

## 自动更新

GitHub Actions 会在每周一和周四（北京时间上午9点）自动运行更新脚本，抓取最新报刊内容。

也可以在仓库的 Actions 页面手动触发更新。

## 本地运行

```bash
pip install -r requirements.txt
python update_database.py
```
