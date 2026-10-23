"""Retired PA routes have no dependency on accounts, runtimes or market data."""
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix='/research/price-action', tags=['retired-price-action'])


@router.api_route('', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@router.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def disabled(path: str = ''):
    raise HTTPException(status_code=410, detail={
        'code': 'PRICE_ACTION_LAB_DISABLED', 'enabled': False,
        'message': 'Price Action Lab has been retired.',
    })
