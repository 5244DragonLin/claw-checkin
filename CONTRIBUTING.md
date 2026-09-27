# 贡献指南

感谢关注 claw-checkin！欢迎以任何形式贡献：

- **提 Issue**：报 Bug、提需求、分享你的接入脚本经验
- **提 PR**：修复问题或新增功能

## 本地开发

```bash
git clone https://github.com/5244DragonLin/claw-checkin.git
cd claw-checkin
pip install -r requirements.txt
cp examples/mock_platform_a.py scripts/
python main.py          # http://127.0.0.1:8317
```

## 约定

- 后端改动请保持「面板只读不写」原则：账本只消费脚本输出，不反向干预脚本
- 前端保持零构建（原生 HTML/CSS/JS），不引入框架与打包流程
- 提交信息格式：`Add: xxx` / `Fix: xxx` / `Optimize: xxx`
- 新增依赖需在 PR 中说明理由，能不加就不加
