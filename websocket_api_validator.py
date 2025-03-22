#!/usr/bin/env python3

import os
import base64
import logging
import json
from pathlib import Path
from dotenv import load_dotenv
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric import ed25519

# 配置日志
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("websocket_api_validator")

def validate_environment():
    """验证环境变量配置"""
    logger.info("检查环境变量配置...")
    
    # 加载环境变量
    load_dotenv(Path(os.path.expanduser("~/Binance/grid_trading_bot/.env")))
    
    api_key = os.getenv('API_KEY')
    private_key_path = os.getenv('PRIVATE_KEY')
    private_key_pass = os.getenv('PRIVATE_KEY_PASS')
    
    issues = []
    
    if not api_key:
        issues.append("API_KEY 未设置")
    else:
        logger.info(f"API_KEY: {api_key[:5]}...{api_key[-5:]}")
        
    if not private_key_path:
        issues.append("PRIVATE_KEY 未设置")
    else:
        logger.info(f"PRIVATE_KEY 路径: {private_key_path}")
        
        # 检查私钥文件是否存在
        if not os.path.isfile(os.path.expanduser(private_key_path)):
            issues.append(f"私钥文件不存在: {private_key_path}")
    
    if private_key_pass and private_key_pass.lower() == "none":
        logger.info("PRIVATE_KEY_PASS 设置为 'None'，将被视为无密码")
    else:
        logger.info(f"PRIVATE_KEY_PASS: {'已设置' if private_key_pass else '未设置'}")
    
    return issues, api_key, private_key_path, private_key_pass

def validate_private_key(private_key_path, private_key_pass=None):
    """验证 Ed25519 私钥的正确性"""
    logger.info("验证私钥...")
    
    issues = []
    private_key = None
    
    try:
        with open(os.path.expanduser(private_key_path), 'rb') as f:
            private_key_data = f.read()
            
        # 处理密码
        if private_key_pass and private_key_pass.lower() != 'none':
            password = private_key_pass.encode('utf-8')
        else:
            password = None
            
        private_key = load_pem_private_key(
            private_key_data,
            password=password
        )
        
        # 检查是否为 Ed25519 私钥
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            issues.append(f"加载的密钥不是 Ed25519 私钥，而是: {type(private_key).__name__}")
            logger.error("私钥不是 Ed25519 类型，这可能是认证失败的原因")
        else:
            logger.info("私钥是有效的 Ed25519 私钥")
            
            # 尝试生成公钥（进一步验证）
            public_key = private_key.public_key()
            logger.info("成功从私钥派生公钥")
            
    except Exception as e:
        issues.append(f"加载私钥时出错: {str(e)}")
        logger.error(f"私钥加载失败: {e}")
    
    return issues, private_key

def test_signature_generation(api_key, private_key):
    """测试签名生成"""
    logger.info("测试签名生成...")
    
    issues = []
    
    if not private_key or not isinstance(private_key, ed25519.Ed25519PrivateKey):
        issues.append("无法测试签名：私钥不可用或类型不正确")
        return issues, None
    
    # 构造用于测试签名的参数
    params = {
        'apiKey': api_key,
        'timestamp': 1710000000000  # 使用固定时间戳便于测试
    }
    
    sorted_params = sorted(params.items())
    query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
    
    try:
        # 生成 Ed25519 签名
        signature = private_key.sign(query_string.encode('utf-8'))
        signature_b64 = base64.b64encode(signature).decode('utf-8')
        
        logger.info(f"测试签名生成成功")
        logger.info(f"查询字符串: {query_string}")
        logger.info(f"Base64 签名: {signature_b64[:10]}...{signature_b64[-10:]}")
        
    except Exception as e:
        issues.append(f"生成签名时出错: {str(e)}")
        logger.error(f"签名生成失败: {e}")
        return issues, None
    
    return issues, signature_b64

def analyze_auth_failure(api_key, signature_b64):
    """分析会话认证失败的可能原因"""
    logger.info("分析认证失败原因...")
    
    issues = []
    recommendations = []
    
    # 1. 检查 API 权限
    issues.append("API 密钥可能没有足够的权限执行会话认证")
    recommendations.append("在 Binance 官网检查 API 密钥的权限设置，确保启用了 WebSocket API 访问")
    
    # 2. API 密钥和私钥匹配问题
    issues.append("API 密钥与私钥可能不匹配")
    recommendations.append("确认当前使用的私钥与创建 API 密钥时注册的公钥相对应")
    
    # 3. 私钥格式问题
    issues.append("私钥可能没有正确格式化为 Ed25519 密钥")
    recommendations.append("验证私钥是否是正确生成的 Ed25519 密钥，可以尝试重新生成新的密钥对")
    
    # 4. IP 限制
    issues.append("您的 IP 地址可能不在 API 密钥的白名单中")
    recommendations.append("检查 Binance API 管理页面中的 IP 限制设置")
    
    # 5. 签名算法问题
    issues.append("WebSocket API 签名算法可能有误")
    recommendations.append("根据官方文档，确保正确计算签名")
    
    # 6. WebSocket API 访问限制
    issues.append("您的账户可能没有启用 WebSocket API 访问权限")
    recommendations.append("联系 Binance 客服确认您的账户是否有 WebSocket API 访问权限")
    
    return issues, recommendations

def check_successful_connection():
    """检查是否能够建立基本的 WebSocket 连接"""
    logger.info("测试基本 WebSocket 连接...")
    
    try:
        import websocket
        ws = websocket.create_connection(
            "wss://ws-api.binance.com:443/ws-api/v3",
            timeout=10
        )
        
        # 发送 ping 请求
        request = {
            "id": "test_ping",
            "method": "ping"
        }
        ws.send(json.dumps(request))
        
        # 获取响应
        response = ws.recv()
        ws.close()
        
        response_data = json.loads(response)
        if response_data.get("status") == 200:
            logger.info("成功连接到 Binance WebSocket API 并收到 ping 响应")
            return True, None
        else:
            logger.error(f"连接成功但 ping 请求失败: {response}")
            return False, f"Ping 请求失败: {response}"
            
    except Exception as e:
        logger.error(f"无法连接到 WebSocket API: {e}")
        return False, f"连接失败: {str(e)}"

def main():
    """主函数"""
    logger.info("开始 Binance WebSocket API 客户端诊断...")
    
    # 步骤 1: 检查环境变量
    env_issues, api_key, private_key_path, private_key_pass = validate_environment()
    
    # 步骤 2: 验证私钥
    if not env_issues:
        key_issues, private_key = validate_private_key(private_key_path, private_key_pass)
    else:
        key_issues, private_key = ["未验证私钥：环境配置有问题"], None
    
    # 步骤 3: 测试签名生成
    if private_key:
        sig_issues, signature = test_signature_generation(api_key, private_key)
    else:
        sig_issues, signature = ["未测试签名：私钥不可用"], None
    
    # 步骤 4: 检查基本连接
    conn_success, conn_error = check_successful_connection()
    
    # 步骤 5: 分析认证失败原因
    auth_issues, auth_recommendations = analyze_auth_failure(api_key, signature)
    
    # 输出总结报告
    logger.info("\n\n===== WebSocket API 客户端诊断报告 =====\n")
    
    if env_issues:
        logger.error("环境配置问题:")
        for issue in env_issues:
            logger.error(f" - {issue}")
            
    if key_issues:
        logger.error("\n私钥问题:")
        for issue in key_issues:
            logger.error(f" - {issue}")
            
    if sig_issues:
        logger.error("\n签名生成问题:")
        for issue in sig_issues:
            logger.error(f" - {issue}")
    
    if not conn_success:
        logger.error(f"\n连接问题: {conn_error}")
    
    logger.info("\n可能导致认证失败的原因:")
    for issue in auth_issues:
        logger.info(f" - {issue}")
    
    logger.info("\n建议:")
    for rec in auth_recommendations:
        logger.info(f" - {rec}")
    
    # 最终诊断
    if not env_issues and not key_issues and not sig_issues and conn_success:
        logger.info("\n基本配置看起来正确。认证失败可能是因为 API 密钥配置或服务器端限制问题。")
    else:
        logger.error("\n发现客户端配置问题，请先解决这些问题再尝试连接。")
        
    logger.info("\n===== 诊断完成 =====")

if __name__ == "__main__":
    main()