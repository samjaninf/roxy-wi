from typing import Union, Literal

from flask import request
from flask.views import MethodView
from flask_pydantic import validate
from flask_jwt_extended import jwt_required

import app.modules.roxywi.common as roxywi_common
import app.modules.db.service as service_sql
import app.modules.service.installation as service_mod
from app.middleware import get_user_params, check_services, page_for_admin, check_group
from app.modules.common.common_classes import SupportClass
from app.modules.roxywi.class_models import BaseResponse, ServiceInstall


class InstallView(MethodView):
    methods = ['POST']
    decorators = [jwt_required(), get_user_params(), check_services, page_for_admin(level=3), check_group()]

    @validate(body=ServiceInstall)
    def post(self, service: Literal['haproxy', 'nginx', 'apache', 'keepalived'], server_id: Union[int, str, None], body: ServiceInstall):
        """
        Install a specific service.
        ---
        tags:
          - Service Installation
        parameters:
          - in: path
            name: service
            schema:
              type: string
              enum: [haproxy, nginx, apache, keepalived]
            required: true
            description: The type of service (haproxy, nginx, apache, keepalived)
          - in: path
            name: server_id
            type: 'integer'
            required: true
            description: The ID or IP of the server
        responses:
          200:
            description: Successful operation
            schema:
              type: 'object'
              properties:
                servers:
                  type: 'array'
                  items:
                    type: 'object'
                    properties:
                      ip:
                        type: 'string'
                      name:
                        type: 'string'
                services:
                  type: 'object'
                  description: 'Which service will be installed.'
                  properties:
                    haproxy:
                      type: 'object'
                      description: 'Status of HAProxy'
                      properties:
                        enabled:
                          type: 'integer'
                        docker:
                          type: 'integer'
                syn_flood:
                  type: 'integer'
                checker:
                  type: 'integer'
                metrics:
                  type: 'integer'
                auto_start:
                  type: 'integer'
          default:
            description: Unexpected error
        """
        try:
            if server_id is not None:
                server_id = SupportClass().return_server_ip_or_id(server_id)
        except Exception as e:
            return roxywi_common.handler_exceptions_for_json_data(e, '')
        try:
            output = service_mod.install_service(service, body)
            if len(output['failures']) > 0 or len(output['dark']) > 0:
                raise Exception(f'Cannot install {service.title()}. Check Apache error log')
            if 'api' in request.url:
                try:
                    service_sql.update_hapwi_server(server_id, body.checker, body.metrics, body.auto_start, service)
                except Exception as e:
                    return roxywi_common.handler_exceptions_for_json_data(e, f'Cannot update Tools settings for {service.title()}')
            return BaseResponse().model_dump(mode='json'), 201
        except Exception as e:
            return roxywi_common.handler_exceptions_for_json_data(e, f'Cannot install {service.title()}')
