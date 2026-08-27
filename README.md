# rivetoo-graph

`rivetoo-graph` 是一个显式、可组合的 Python DAG 声明与执行库。

它提供图、步骤、条件依赖、同步与异步执行、运行状态、结果读取和执行视图渲染。
库本身不依赖宿主的日志、终端或请求状态；宿主能力通过独立端口显式接入。

Python 导入入口为：

```python
from rivetoo_graph import Graph, GraphPlan, Step
```

完整的模型、组合与执行说明见 [Graph 文档](./docs/index.md)。
