use tradelogx_nexus::{Client, DecisionQuery};

fn main() -> Result<(), tradelogx_nexus::Error> {
    let c = Client::from_env()?;
    let s = c.strategies()?;
    let n = c.decisions(DecisionQuery { limit: Some(5), ..Default::default() }).count();
    let p = c.positions()?;
    println!("{} strategies; {} decisions; {} positions", s.len(), n, p.len());
    if let Err(e) = c.promote("brain", "live") {
        println!("promote live -> {} {}", e.status, e.code);
    }
    Ok(())
}
