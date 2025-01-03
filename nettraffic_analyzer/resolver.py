# @Author  : yuanzi
# @Time    : 2024/11/17 15:31
# Website: https://www.yzgsa.com
# Copyright (c) <yuanzigsa@gmail.com>
import json
from enum import Enum
import logging
import re
from nettraffic_analyzer.xdbSearcher import XdbSearcher
from nettraffic_analyzer.utils import setup_logger, ipv6_search

logger = logging.getLogger(__name__)


class Isp(Enum):
    CHINA_MOBILE = "中国移动"
    CHINA_UNICOM = "中国联通"
    CHINA_TELECOM = "中国电信"


class Resolver:
    def __init__(self):
        dbPath = "res/china.xdb"
        self.cb = XdbSearcher.loadContentFromFile(dbfile=dbPath)

    @staticmethod
    def resolve_ip_region(original_content, ipv6=False):
        """
        解析xdb原始查询内容，返回省份、城市、区县、运营商信息
        """
        if not original_content:
            return {}

        if ipv6 and len(original_content) > 15:
            return {
                'province': original_content[13],
                'city': original_content[15],
                # 'district': original_content[13],
                'isp': original_content[6],
            }

        # 默认解析ipv4查询结果
        parts = original_content.split('|')
        return {
            'province': parts[2] if len(parts) > 2 else None,
            'city': parts[3] if len(parts) > 3 else None,
            'district': parts[4] if len(parts) > 4 else None,
            'isp': parts[-3] if len(parts) >= 3 else None,
        }

    @staticmethod
    def is_ipv4(ip):
        ipv4_pattern = re.compile(
            r'^(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}$')
        return bool(ipv4_pattern.match(ip))

    @staticmethod
    def get_node_and_customer(ip, interface, data):
        try:
            for item in data:
                if ip == item['agent_ip'] and item['interface'] == interface:
                    return item['node'], item['costumer'], item['switch']
            else:
                return "未知",  "未知", "未知"
        except Exception as e:
            logger.error(f"Error in get_node_and_customer: {e}")
            return "未知",  "未知", "未知"

    @staticmethod
    def read_config_data():
        try:
            with open('res/config_data.json', 'r') as f:
                data = json.load(f)
                logger.info(f"当前配置：{data}")
                return data

        except Exception as e:
            logger.error(f"Error in read_config_data: {e}")
            return []


    @staticmethod
    def _get_agent_ip(data, host_ip, interface):
         for item in data:
             if item['host_ip'] == host_ip and item['interface'] == interface:
                 return item['agent_ip']


    def rewrite_docs(self, docs):
        """
        重写elasticsearch查询结果，添加IP归属地信息
            1. 同运营商省内比例-同网省内
            2. 同运营商出省比例-同网跨省
            3. 去往移动的比例-异网(移动)
            4. 去往联通的比例-异网(联通)
            5. 去往电信的比例-异网(电信)
        """
        searcher = XdbSearcher(contentBuff=self.cb)
        config_data = self.read_config_data()
        new_docs = []
        for doc in docs:
            source = doc['_source']
            # 默认情况下agent_ip和host_ip是一样的，但在三线情况下可能不同，所以以agent_ip为准
            host_ip = source['host'].get('ip')
            dst_ip = source.get('dst_ip')
            ifindex = source.get('input_interface_value')
            agent_ip = next((item['agent_ip'] for item in config_data if
                             item['host_ip'] == host_ip and item['interface'] == ifindex), None)
            if dst_ip is None or host_ip is None or agent_ip is None:
                continue

            # 查询agent_ip的归属地信息
            result = searcher.search(agent_ip)
            agent_ip_info = self.resolve_ip_region(result)

            if self.is_ipv4(dst_ip):
                result = searcher.search(dst_ip)
                dst_ip_info = self.resolve_ip_region(result)
                source['ipType'] = "ipv4"
            else:
                # ipv6
                result = ipv6_search(dst_ip)
                dst_ip_info = self.resolve_ip_region(result, ipv6=True)
                source['ipType'] = "ipv6"
            # 判断同网还是异网
            agent_isp = agent_ip_info.get('isp') if agent_ip_info.get('isp') is not None else "未知"
            dst_isp = dst_ip_info.get('isp') if dst_ip_info.get('isp') is not None else "未知"
            agent_province = agent_ip_info.get('province') if agent_ip_info.get('province') is not None else "未知"
            dst_province = dst_ip_info.get('province') if dst_ip_info.get('province') is not None else "未知"
            agent_isp = agent_isp.replace('中国', '')
            dst_isp = dst_isp.replace('中国', '')
            if agent_isp != "未知" and dst_isp != "未知" and agent_isp == dst_isp:
                # 同网
                if agent_province and dst_province and agent_province == dst_province:
                    source['flow_isp_type'] = '同网省内'
                else:
                    source['flow_isp_type'] = '同网跨省'
            else:
                # 异网
                if not dst_isp:
                    source['flow_isp_type'] = '异网(未知)'
                else:
                    source['flow_isp_type'] = f'异网({dst_isp})'

            source['flow_isp_info'] = dst_ip_info
            # 添加节点信息
            interface = source.get('input_interface_value')
            node, customer, sw_interface = self.get_node_and_customer(agent_ip, interface, config_data)
            source['node'] = node
            source['customer'] = customer
            source['sw_interface'] = sw_interface
            source['dst_ip_region'] = f"{dst_ip} {dst_ip_info.get('province', '')}{dst_ip_info.get('city', '')}{dst_isp}"
            doc['_source'] = source
            new_docs.append(doc)
            # logger.info(f"IP:{ip.ljust(18)}归属：{province}-{city}-{isp}")

        # 关闭searcher
        searcher.close()
        return new_docs
