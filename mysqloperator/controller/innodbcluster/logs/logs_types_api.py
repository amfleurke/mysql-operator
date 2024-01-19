# Copyright (c) 2023, Oracle and/or its affiliates.
#
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
#

from enum import Enum
from typing import Optional, Union, List, Dict
from logging import Logger
from ...api_utils import dget_bool, dget_int, ApiSpecError
from ...kubeutils import client as api_client
from ... import utils
import yaml
from abc import ABC, abstractmethod

def get_object_name(obj: Union[Dict, api_client.V1Container, api_client.V1Volume]) -> str:
    # When we get data from K8s it will be V1Container/V1Volume, however, as we patch the STS
    # the data may become Dict, because this is what merge_patch_object() produces
    # There are multiple log types and every each one of them works on the 'logcollector'
    # container as well as on the volume being mounted. The first one will see
    # V1Container, the next will see Dict.
    return obj["name"] if isinstance(obj, dict) else obj.name

# Must correspond to the names in the CRD
class ServerLogType(Enum):
    ERROR = "error"
    GENERAL = "general"
    SLOW_QUERY = "slowQuery"

class ConfigMapMountBase(ABC):
    def __init__(self, volume_mount_name: str, config_file_name: str, config_file_mount_path: str):
        super().__init__()
        self.volume_mount_name = volume_mount_name
        self.config_file_name = config_file_name
        self.config_file_mount_path = config_file_mount_path

    @abstractmethod
    def parse(self, spec: dict, prefix: str, logger: Logger) -> None:
        ...

    @abstractmethod
    def validate(self) -> None:
        ...

    def _add_volumes_to_sts_spec(self,
                                 sts: Union[dict, api_client.V1StatefulSet],
                                 cm_name: str,
                                 logger: Logger) -> None:
        patch = {
            "volumes" : [
                {
                    "name": self.volume_mount_name,
                    "configMap": {
                        "name" : cm_name,
                        "defaultMode": 0o644,
                        "items": [
                            {
                                "key" : self.config_file_name,
                                "path": self.config_file_name
                            }
                        ]
                    }
                }
            ]
        }

        # During first time creation
        if isinstance(sts, dict):
            utils.merge_patch_object(sts["spec"]["template"]["spec"], patch)
        elif isinstance(sts, api_client.V1StatefulSet):
            # first filter out our old logs volumes spec
            sts.spec.template.spec.volumes = [volume for volume in sts.spec.template.spec.volumes if volume and get_object_name(volume) != self.volume_mount_name]
            sts.spec.template.spec.volumes += patch["volumes"]

    def _add_containers_to_sts_spec(self,
                                    sts: Union[dict, api_client.V1StatefulSet],
                                    container_name: str,
                                    logger: Logger) -> None:
        patch = {
            "containers" : [
                {
                    "name": container_name,
                    "volumeMounts": [
                        {
                            "name" : self.volume_mount_name,
                            "mountPath": f"{self.config_file_mount_path}/{self.config_file_name}",
                            "subPath": self.config_file_name
                        }
                    ]
                }
            ]
        }

        if isinstance(sts, dict):
            utils.merge_patch_object(sts["spec"]["template"]["spec"], patch)
        elif isinstance(sts, api_client.V1StatefulSet):
            found = False
            for container in sts.spec.template.spec.containers:
                if get_object_name(container) == container_name:
                    container.volume_mounts = [vm for vm in container.volume_mounts if vm and get_object_name(vm) != self.volume_mount_name]
                    container.volume_mounts += patch["containers"][0]["volumeMounts"]
                    found = True
                    break
            if not found:
                sts.spec.template.spec.containers += patch

    def add_to_sts_spec(self,
                        sts: Union[dict, api_client.V1StatefulSet],
                        container_name: str,
                        cm_name: str,
                        logger: Logger) -> None:
        self._add_containers_to_sts_spec(sts, container_name, logger)
        self._add_volumes_to_sts_spec(sts, cm_name, logger)


class MySQLLogSpecBase(ConfigMapMountBase):
    def __init__(self, volume_mount_name: str, config_file_name: str, config_file_mount_path: str):
        super().__init__(volume_mount_name, config_file_name, config_file_mount_path)

    @abstractmethod
    def get_cm_data(self, logger: Logger) -> Dict[str, str]:
        ...

class GeneralLogSpec(MySQLLogSpecBase):
    def __init__(self):
        super().__init__("general-log-config", "general-log.cnf", "/etc/my.cnf.d")
        self._enabled : Optional[bool] = None
        self.collect: bool = False
        self.fileName: str = "general_query.log"
        self._prefix: Optional[str] = None

    def parse(self, spec: dict, prefix: str, logger: Logger) -> None:
        if not spec:
            return

        self._prefix = prefix

        field = "enabled"
        if field in spec:
            self._enabled = dget_bool(spec, field, prefix)

        field = "collect"
        if field in spec:
            self.collect = dget_bool(spec, field, prefix)

    def validate(self) -> None:
        if self.collect and not self.enabled:
            raise ApiSpecError(f"{self._prefix}.collect is enabled while {self._prefix}.enabled is not")

    @property
    def enabled(self) -> Optional[bool]:
        return self._enabled

    def get_cm_data(self, logger: Logger) -> Dict[str, str]:
        mycnf = f"""# Generated by MySQL Operator for Kubernetes
[mysqld]
general_log={1 if self.enabled else 0}"""

        if self.enabled:
            mycnf += f"""
general_log_file={self.fileName}"""

        return {
            self.config_file_name : mycnf
        }


class ErrorLogSpec(MySQLLogSpecBase):
    def __init__(self):
        super().__init__("error-log-config", "error-log.cnf", "/etc/my.cnf.d")
        self.collect: bool = False
        self.verbosity: int = 3
        self.error_log_name: str = "error.log"
        self._prefix: Optional[str] = None

    def parse(self, spec: dict, prefix: str, logger: Logger) -> None:
        if not spec:
            return
        self._prefix = prefix

        field = "verbosity"
        if field in spec:
            self.verbosity = dget_int(spec, field, prefix)

        field = "collect"
        if field in spec:
            self.collect = dget_bool(spec, field, prefix)

    def validate(self) -> None:
        if self.verbosity < 1 or self.verbosity > 3:
            raise ApiSpecError(f"{self._prefix}.verbosity must be between 1 and 3")

    @property
    def enabled(self) -> bool:
        return True

    def get_cm_data(self, logger: Logger) -> Dict[str, str]:
        mycnf = f"""# Generated by MySQL Operator for Kubernetes
[mysqld]
log_error_verbosity={self.verbosity}"""

        if self.collect:
            mycnf += f"""
log_error='{(self.error_log_name)}'
log_error_services='log_sink_json'"""
        return {
            self.config_file_name : mycnf
        }


class SlowQueryLogSpec(MySQLLogSpecBase):
    def __init__(self):
        super().__init__("slow-query-log-config", "slow-query-log.cnf", "/etc/my.cnf.d")
        self._enabled: Optional[bool] = None
        self.longQueryTime: Optional[Union[float, int]] = None
        self.collect: bool = False
        self.fileName: str = "slow_query.log"
        self._prefix: Optional[str] = None

    def parse(self, spec: dict, prefix: str, logger: Logger) -> None:
        if not spec:
            return

        self._prefix = prefix

        field = "enabled"
        if field in spec:
            self._enabled = dget_bool(spec, field, prefix)

        field ="longQueryTime"
        if field in spec:
            # dget_float() doesn't like when an integer is passed and there is no dget_int_or_float
            # dget_str() doesn't like to read an integer and longQueryTime is "number" and not a "string" in the CRD
            # Thus direct read without going over dget_ . We have checked that it is there and the default value
            # comes from the CRD anyway
            self.longQueryTime = spec[field]

        field = "collect"
        if field in spec:
            self.collect = dget_bool(spec, field, prefix)

    def validate(self) -> None:
        if (isinstance(self.longQueryTime, int) or isinstance(self.longQueryTime, float)) and self.longQueryTime < 0:
            raise ApiSpecError(f"{self._prefix}.longQueryTime must not be negative")
        if self._prefix and self.collect and not self.enabled:
            raise ApiSpecError(f"{self._prefix}.collect is enabled while {self._prefix}.enabled is not")

    @property
    def enabled(self) -> Optional[bool]:
        return self._enabled

    def get_cm_data(self, logger: Logger) -> Dict[str, str]:
        mycnf = f"""# Generated by MySQL Operator for Kubernetes
[mysqld]
slow_query_log={1 if self.enabled else 0}"""

        if self.enabled:
            mycnf += f"""
slow_query_log_file='{self.fileName}'
log_slow_admin_statements=1"""

            if self.longQueryTime:
                mycnf += f"""
long_query_time={self.longQueryTime}"""

        return {
            self.config_file_name : mycnf
        }
