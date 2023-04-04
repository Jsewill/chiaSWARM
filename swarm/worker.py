from .gpu.device import Device
from .generator import do_work
from .log_setup import setup_logging
from . import __version__
from .gpu.device_pool import add_device_to_pool, remove_device_from_pool
from .settings import (
    load_settings,
    resolve_path,
)
import torch
import asyncio
import logging
from datetime import datetime
import json
from packaging import version
import aiohttp

settings = load_settings()
hive_uri = f"{settings.sdaas_uri.rstrip('/')}/api"


async def run_worker():
    await startup()

    logging.info("worker")

    wait_seconds = 0
    while True:
        await asyncio.sleep(wait_seconds)
        device = remove_device_from_pool()  # this will block if all gpus are busy

        try:
            wait_seconds = await ask_for_work(device)

        except Exception as e:
            print(e)
            wait_seconds = 121

        finally:
            add_device_to_pool(device)


async def handle_status_200(response, device, session):
    try:
        response_dict = await response.json()
    except json.JSONDecodeError:
        print(f"Error: Unable to decode server response: {await response.text()}")
        return 11

    if "jobs" not in response_dict:
        print("Error: 'jobs' field is missing in the server response")
        return 11

    if not isinstance(response_dict["jobs"], list):
        print("Error: 'jobs' field is not a list in the server response")
        return 11

    did_work = False
    for job in response_dict["jobs"]:
        await spawn_task(job, device, session)
        did_work = True

    # if we did work, ask for more right away, otherwise wait 11 seconds
    return 0 if did_work else 11


async def handle_status_400(response):
    response_dict = await response.json()
    message = response_dict.pop("message", "bad worker")
    print(f"{hive_uri} says {message}")
    response.raise_for_status()


async def ask_for_work(device):
    print(
        f"{datetime.now()}: Device {device.device_id} asking for work from the hive at {hive_uri}..."
    )
    mem_info = torch.cuda.mem_get_info(device.device_id)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"{hive_uri}/work",
                    timeout=aiohttp.ClientTimeout(total=10),
                    params={
                        "worker_version": __version__,
                        "worker_name": f"{settings.worker_name}:{device.device_id}",
                        "vram": mem_info[1],
                    },
                    headers={
                        "Content-type": "application/json",
                        "Authorization": f"Bearer {settings.sdaas_token}",
                        "user-agent": f"chiaSWARM.worker/{__version__}",
                    },
            ) as response:

                if response.status == 200:
                    return await handle_status_200(response, device, session)

                elif response.status == 400:
                    await handle_status_400(response)

                else:
                    print(f"{hive_uri} returned {response.status}")
                    response.raise_for_status()

                return 11

    except aiohttp.ClientConnectorError as e:
        print(f"Error: Could not connect to the server: {e}")
        return 121

    except aiohttp.ClientError as e:
        print(f"Error: An unexpected error occurred while making the request: {e}")
        return 121

    except asyncio.TimeoutError:
        print("Error: Request timed out")
        return 121

    except Exception as e:
        print(f"Error: An unknown error occurred: {e}")
        return 121


async def spawn_task(job, device, session):
    print(f"Device {device.device_id} got work")

    try:
        # main worker function
        result = await do_work(job, device)

        async with session.post(
                f"{hive_uri}/results",
                data=json.dumps(result),
                headers={
                    "Content-type": "application/json",
                    "Authorization": f"Bearer {settings.sdaas_token}",
                    "user-agent": f"chiaSWARM.worker/{__version__}",
                },
        ) as resultResponse:
            if resultResponse.status == 500:
                print(f"The hive returned an error: {await resultResponse.text()}")
            else:
                print(f"Device {device.device_id} {await resultResponse.json()}")

    except Exception as e:
        print(f"Error: An exception occurred while processing the job or submitting the results: {e}")


async def startup():
    if not torch.cuda.is_available():
        raise Exception("CUDA not present. Quitting.")

    if version.parse(torch.__version__) < version.parse("2.0.0"):
        raise Exception(
            f"Pytorch must be 2.0 or greater (found {torch.__version__}). Run install script. Quitting."
        )

    setup_logging(resolve_path(settings.log_filename), settings.log_level)
    logging.info(f"Version {__version__}")
    logging.debug(f"Torch version {torch.__version__}")

    torch.set_float32_matmul_precision("high")
    torch.backends.cudnn.benchmark = True  # type: ignore
    torch.backends.cuda.matmul.allow_tf32 = True  # type: ignore

    for i in range(0, torch.cuda.device_count()):
        logging.info(f"Adding cuda device {i} - {torch.cuda.get_device_name(i)}")
        add_device_to_pool(Device(i))


if __name__ == "__main__":
    asyncio.run(run_worker())
