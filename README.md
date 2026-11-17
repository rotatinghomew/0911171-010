# 定时指标简报

容器环境固定在仓库 Dockerfile 的 Python 3.11.14 镜像摘要，时区数据包固定为 requirements.txt 的版本。执行 `docker build -t local-analytics .` 后，`docker run --rm --network none local-analytics` 运行内置例子；`docker run --rm --network none local-analytics python -m unittest discover -s tests -v` 运行测试。构建时安装依赖，运行时无需联网；非容器环境使用 Python 3.11，先执行 `python -m pip install -r requirements.txt`，并把 `PYTHONTZPATH` 设为空字符串以使用锁定的时区数据而非宿主机数据库。

该本地原型按指标定义计算合成日报，并模拟定时发送记录，不连接邮件服务。

运行 `python -m unittest discover -s tests -v`；运行 `python -m digest.demo`。演示记录只保存在进程内存，发送内容为样例数据。

`digest.mailbox.LocalMailbox` 是本地邮件接收端，按投递编号和收件人保存 JSON 文件，重复投递相同内容返回同一回执，不同内容复用编号会被拒绝。`send_digest` 将当前分发器输出交给邮箱；邮箱可重新打开查看历史消息，而 `Dispatcher` 的运行记录仍只保存在内存。当前调度器按 UTC 整点，指标仅支持日内单字段的总和或均值。测试使用临时目录，不访问邮件网络。
