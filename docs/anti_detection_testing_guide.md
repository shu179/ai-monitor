# 反检测测试指南

## 测试目的

验证当前反检测系统在专业爬虫检测网站上的表现，评估是否能被识别为真实用户。

## 测试网站

### 1. Bot.Sannysoft (https://bot.sannysoft.com/)
**检测项目**:
- ✅ User-Agent
- ✅ navigator.webdriver
- ✅ navigator.plugins
- ✅ navigator.languages
- ✅ WebGL Vendor
- ✅ Chrome 对象
- ✅ Permissions API
- ✅ DevTools 检测

**期望结果**: 所有项目显示绿色（正常）

---

### 2. AreYouHeadless (https://arh.antoinevastel.com/bots/areyouheadless)
**检测项目**:
- ✅ Headless Chrome 特征
- ✅ User-Agent 一致性
- ✅ Plugins 长度
- ✅ WebDriver 属性

**期望结果**: "You are not Chrome headless"

---

### 3. PixelScan (https://pixelscan.net/)
**检测项目**:
- ✅ Canvas 指纹
- ✅ WebGL 指纹
- ✅ Audio 指纹
- ✅ 字体指纹
- ✅ 屏幕分辨率
- ✅ 时区

**期望结果**: 
- Consistency Score > 90%
- Bot Score < 10%

---

### 4. CreepJS (https://abrahamjuliot.github.io/creepjs/)
**检测项目**:
- ✅ 深度指纹分析
- ✅ Lies 检测（伪造检测）
- ✅ Trust Score

**期望结果**: 
- Trust Score > 80%
- Lies 数量 < 5

---

### 5. DeviceAndBrowserInfo (https://deviceandbrowserinfo.com/are_you_a_bot)
**检测项目**:
- ✅ 综合 Bot 检测
- ✅ 浏览器信息一致性

**期望结果**: "You are not a bot"

---

## 运行测试

### 方法1：使用测试脚本（推荐）

```bash
cd /Users/shuao/Desktop/ai-monitor
python test_anti_detection.py
```

脚本会自动：
1. 启动浏览器（前台显示）
2. 依次访问5个检测网站
3. 等待你查看结果
4. 按 Enter 继续下一个

### 方法2：手动测试

```bash
cd /Users/shuao/Desktop/ai-monitor
python main.py --inspect
```

然后手动访问上述网站。

---

## 评估标准

### 🟢 优秀（90-100分）
- Bot.Sannysoft: 所有项目绿色
- AreYouHeadless: 未检测到 Headless
- PixelScan: Bot Score < 10%
- CreepJS: Trust Score > 80%
- DeviceAndBrowserInfo: 未检测到 Bot

### 🟡 良好（70-89分）
- Bot.Sannysoft: 1-2个黄色警告
- PixelScan: Bot Score 10-30%
- CreepJS: Trust Score 60-80%

### 🔴 需要改进（<70分）
- Bot.Sannysoft: 3个以上红色
- PixelScan: Bot Score > 30%
- CreepJS: Trust Score < 60%

---

## 常见问题

### Q1: Bot.Sannysoft 显示 "navigator.webdriver: true"
**原因**: JS 注入失效或被覆盖  
**解决**: 检查 `platforms/base.py:546-551` 的 webdriver 覆盖

### Q2: PixelScan 显示 "Inconsistent fingerprint"
**原因**: User-Agent 与实际浏览器特征不一致  
**解决**: 检查 `browser_fingerprint.py` 的 UA 生成逻辑

### Q3: CreepJS 显示大量 "Lies"
**原因**: 过度伪造被检测  
**解决**: 减少 JS 注入，依赖 Patchright CDP 层保护

### Q4: 所有网站都显示正常，但中国AI平台仍触发验证码
**原因**: 
- 查询频率过高
- 行为模式固定
- 账号历史记录异常

**解决**:
- 增加查询间隔（≥30分钟）
- 使用多样化关键词
- 更换新账号测试

---

## 测试记录模板

```
测试日期: 2026-04-21
测试环境: macOS / Windows / Linux
浏览器模式: 有头 / 无头

| 网站 | 结果 | 评分 | 备注 |
|------|------|------|------|
| Bot.Sannysoft | ✅/⚠️/❌ | /100 | |
| AreYouHeadless | ✅/❌ | - | |
| PixelScan | ✅/⚠️/❌ | /100 | Bot Score: X% |
| CreepJS | ✅/⚠️/❌ | /100 | Trust Score: X% |
| DeviceAndBrowserInfo | ✅/❌ | - | |

综合评分: /100
综合评级: 🟢优秀 / 🟡良好 / 🔴需改进
```

---

## 下一步

根据测试结果：

1. **如果评分 > 90**: 
   - ✅ 反检测系统优秀，可投入生产
   - 继续监控中国AI平台的实际表现

2. **如果评分 70-90**:
   - ⚠️ 反检测系统良好，但有改进空间
   - 根据具体失败项进行针对性优化

3. **如果评分 < 70**:
   - ❌ 反检测系统需要改进
   - 检查 Patchright 是否正确安装
   - 检查 JS 注入是否生效
   - 考虑启用全平台有头模式

---

**测试脚本**: [test_anti_detection.py](test_anti_detection.py)  
**最后更新**: 2026-04-21
